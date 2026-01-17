"""Robust extractor for KSEI/BEI "Kepemilikan Efek di Atas 5%" tables.

Why this exists
--------------
The existing `extract_blue_fast.py` is optimized for a *blue* text-table format.
The sample PDF provided in this project contains a *black* multi-column table.
Text extraction is tricky because:
  - columns are not separated by consistent whitespace,
  - some fields wrap (Alamat/Alamat Lanjutan),
  - running number may appear only on first line of a record,
  - layered text can duplicate words/glyphs.

This script uses pdfplumber word positions (x/y) and a fixed column layout
inferred from the header row to bucket words into stable columns.

Output
------
Writes a clean Excel with one row per ownership record.
Also optionally writes debug artifacts (per-page word coordinates).

Usage
-----
python extract_ownership_table.py --pdf "/path/file.pdf" --out outputs/extracted/ownership_table.xlsx
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import pdfplumber


TICKER_RE = re.compile(r"(?<![A-Z])([A-Z]{4})(?![A-Z])")
ROWNO_RE = re.compile(r"^\s*(\d{1,5})\b")
INT_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
# Also allow plain integers (some rows contain small values like 0 / 495)
INT_ANY_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?!\d)")
PCT_RE = re.compile(r"(?<!\d)(\d{1,3})\.(\d{2})(?!\d)")

COUNTRY_HINTS = {
    "INDONESIA",
    "SINGAPORE",
    "MALAYSIA",
    "UNITED",
    "KINGDOM",
    "UNITEDSTATES",
    "AMERICA",
    "HONG",
    "KONG",
    "CHINA",
    "JAPAN",
    "KOREA",
    "THAILAND",
    "PHILIPPINES",
    "VIETNAM",
    "TAIWAN",
    "AUSTRALIA",
    "NETHERLANDS",
    "GERMANY",
    "FRANCE",
    "SWITZERLAND",
    "LUXEMBOURG",
}



def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


# --- PATCH: collapse spaced-out glyph extraction artifacts ---
def _compact_spaced_text(s: str) -> str:
    """Collapse spaced-out glyph extraction artifacts.

    Examples:
      - "I N D O N E S I A" -> "INDONESIA"
      - "5 9 9 , 4 9 1 , 2 2 8" -> "599,491,228"
      - "4 1 . 1 0" -> "41.10"
    """
    if not s:
        return ""
    s = str(s)

    # Collapse sequences of single-letter uppercase tokens.
    # e.g. "I N D O N E S I A" -> "INDONESIA"
    def _collapse_letters(m: re.Match) -> str:
        return m.group(0).replace(" ", "")

    s = re.sub(r"\b(?:[A-Z]\s+){2,}[A-Z]\b", _collapse_letters, s)

    # Collapse runs of spaced-out digit/punctuation tokens WITHOUT merging distinct numbers.
    # We only join sequences that look like single-character digit/punct tokens (e.g. "5 9 9 , 4 9 1 , 2 2 8").
    toks = s.split()
    out = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        # A "spaced glyph" token is a single digit or a single punctuation (comma/dot)
        def is_glyph(t: str) -> bool:
            return (len(t) == 1) and (t.isdigit() or t in {",", "."})

        if is_glyph(tok):
            j = i
            run = []
            while j < len(toks) and is_glyph(toks[j]):
                run.append(toks[j])
                j += 1
            # Only collapse if the run has at least 3 digits total (avoid collapsing "1 . 2" weirdly)
            digit_count = sum(1 for t in run if t.isdigit())
            if digit_count >= 3:
                out.append("".join(run))
            else:
                out.extend(run)
            i = j
            continue

        out.append(tok)
        i += 1

    s = " ".join(out)

    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- PATCH: helpers for robust nat/dom/status extraction when headers are merged/graphical ---
def _is_country_token(t: str) -> bool:
    t = _compact_spaced_text(t or "").strip().upper()
    if not t:
        return False
    # Exact match
    if t in COUNTRY_HINTS:
        return True
    # Common glue cases
    if "INDONESIA" in t or "SINGAPORE" in t or "MALAYSIA" in t:
        return True
    return False


def _refine_nat_dom_status_from_positions(
    row_words: List[Dict[str, Any]],
    page_width: float,
    bucket_text: Dict[str, str],
) -> None:
    """Fix nationality/domicile/status using x-positions.

    Some PDFs have merged/graphical headers so our header-based bands default and may be off.
    We recover:
      - status: pick the best 'L'/'A' token near the expected status area (right of domicile, left of numeric block)
      - domicile: last country token before status, near the right side of the text columns
      - nationality: previous country token (if any) further left
    This is conservative: only overwrites when we find high-confidence signals.
    """
    # Build candidates (text, xmid)
    cands: List[Tuple[str, float]] = []
    for w in row_words:
        txt = _compact_spaced_text(w.get("text", "")).strip()
        if not txt:
            continue
        xmid = _word_mid_x(w)
        cands.append((txt, xmid))

    # 1) Find numeric block start as the left-most "big number" x, else use a fallback.
    num_xs = []
    for txt, x in cands:
        if INT_THOUSANDS_RE.search(_compact_spaced_text(txt)):
            num_xs.append(x)
        elif PCT_RE.search(_compact_spaced_text(txt)):
            num_xs.append(x)
    numeric_start_x = min(num_xs) if num_xs else (0.70 * page_width)

    # 2) Status candidate: single letter L/A that sits before numeric block and after mid columns.
    status_cands = [
        (txt.upper(), x)
        for txt, x in cands
        if txt.strip().upper() in {"L", "A"} and (0.45 * page_width) <= x <= (numeric_start_x - 2)
    ]
    # Prefer the right-most such token (closest to numeric block)
    status_val = ""
    status_x = None
    if status_cands:
        status_val, status_x = sorted(status_cands, key=lambda p: p[1])[-1]

    # 3) Domicile: last country token before status (or before numeric_start_x if status missing)
    right_edge = status_x if status_x is not None else numeric_start_x
    country_cands = [(txt, x) for txt, x in cands if _is_country_token(txt) and x < right_edge]
    domicile_val = ""
    domicile_x = None
    if country_cands:
        domicile_val, domicile_x = sorted(country_cands, key=lambda p: p[1])[-1]

    # 4) Nationality: previous country token further left (if any)
    nationality_val = ""
    if domicile_x is not None:
        left_country = [(txt, x) for txt, x in country_cands if x < (domicile_x - 5)]
        if left_country:
            nationality_val = sorted(left_country, key=lambda p: p[1])[-1][0]

    # 5) Apply conservative overwrites
    if status_val:
        bucket_text["status"] = status_val

    if domicile_val:
        # If domicile bucket is empty or obviously wrong (contains long address), overwrite.
        cur = (bucket_text.get("domicile", "") or "").strip()
        if (not cur) or (len(cur) > 30) or (not _is_country_token(cur)):
            bucket_text["domicile"] = domicile_val

    if nationality_val:
        cur = (bucket_text.get("nationality", "") or "").strip()
        if (not cur) or (len(cur) > 30) or (not _is_country_token(cur)):
            bucket_text["nationality"] = nationality_val


def dedupe_words(words: List[Dict[str, Any]], ndigits: int = 2) -> List[Dict[str, Any]]:
    """De-dupe duplicated words caused by layered text.

    We key by (text, rounded bbox). This is similar to `dedupe_chars` in extract_blue_fast.py
    but at word-level.
    """
    seen = set()
    out = []
    for w in words:
        txt = w.get("text")
        if not txt:
            continue
        key = (
            txt,
            round(float(w.get("x0", 0.0)), ndigits),
            round(float(w.get("x1", 0.0)), ndigits),
            round(float(w.get("top", 0.0)), ndigits),
            round(float(w.get("bottom", w.get("top", 0.0))), ndigits),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _word_mid_x(w: Dict[str, Any]) -> float:
    return (float(w["x0"]) + float(w["x1"])) / 2.0


def _word_mid_y(w: Dict[str, Any]) -> float:
    return (float(w["top"]) + float(w.get("bottom", w["top"]))) / 2.0


def cluster_rows(words: List[Dict[str, Any]], y_tol: float) -> List[List[Dict[str, Any]]]:
    """Cluster words into visual rows using y-mid with a tight tolerance."""
    words = sorted(words, key=lambda w: (_word_mid_y(w), float(w["x0"])))
    rows: List[List[Dict[str, Any]]] = []
    refs: List[float] = []
    for w in words:
        y = _word_mid_y(w)
        if not rows:
            rows.append([w])
            refs.append(y)
            continue
        if abs(y - refs[-1]) <= y_tol:
            rows[-1].append(w)
            # update ref as running median-ish: average is ok with tight tol
            refs[-1] = (refs[-1] * 0.7) + (y * 0.3)
        else:
            rows.append([w])
            refs.append(y)
    return rows


@dataclass
class ColumnBands:
    # left edges (increasing). right edge is next left, last uses page width
    cuts: List[Tuple[str, float]]

    def bucket(self, x_mid: float, page_width: float) -> str:
        cuts = self.cuts
        for i in range(len(cuts)):
            name, x0 = cuts[i]
            x1 = cuts[i + 1][1] if i + 1 < len(cuts) else page_width + 1
            if x_mid >= x0 and x_mid < x1:
                return name
        return cuts[-1][0]


def infer_bands_from_header(page: pdfplumber.page.Page) -> ColumnBands:
    """Infer column cutpoints from the header word positions.

    The header is stable across pages in these KSEI exports.
    We only need approximate x cutpoints.
    """
    # header lives near top (~20-45). Keep all words in that band.
    hdr_words = [w for w in page.extract_words(use_text_flow=True) if float(w["top"]) < 60]
    hdr_words = dedupe_words(hdr_words)

    def x_of(label: str, default: float) -> float:
        for w in hdr_words:
            if w["text"].strip().upper() == label:
                return float(w["x0"])
        return default

    # fallbacks are based on common BEI layout (A4 landscape-ish).
    cuts = [
        ("row_no", x_of("NOKODE", 25.0)),
        ("ticker", x_of("EFEK", 35.0)),
        ("emiten", x_of("NAMA", 70.0)),  # first NAMA after kode efek
        ("broker", x_of("PEMEGANG", 130.0)),
        ("shareholder", 205.0),
        ("account_name", 280.0),
        ("address", x_of("ALAMAT", 360.0)),
        ("address2", 425.0),
        ("nationality", x_of("KEBANGSAAN", 475.0)),
        ("domicile", x_of("DOMISILI", 510.0)),
        ("status", x_of("STATUS", 535.0)),
        ("shares_before", 556.0),
        ("combined_before", 578.0),
        ("pct_before", 612.0),
        ("shares_after", 657.0),
        ("combined_after", 676.0),
        ("pct_after", 710.0),
        ("delta", 752.0),
    ]
    # Ensure strictly increasing (some x_of could return same). If not, enforce monotonic.
    fixed: List[Tuple[str, float]] = []
    last = -1e9
    for name, x in cuts:
        x = float(x)
        if x <= last:
            x = last + 5.0
        fixed.append((name, x))
        last = x
    return ColumnBands(fixed)


def join_bucket(words: List[Dict[str, Any]]) -> str:
    if not words:
        return ""
    words = sorted(words, key=lambda w: float(w["x0"]))
    joined = " ".join(w["text"] for w in words)
    joined = _compact_spaced_text(joined)
    return _norm(joined)


def parse_int_token(s: str) -> Optional[int]:
    if not s:
        return None
    s = _compact_spaced_text(s)
    m = INT_ANY_RE.search(s)
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def parse_pct_token(s: str) -> Optional[float]:
    if not s:
        return None
    s = _compact_spaced_text(s)
    m = PCT_RE.search(s)
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2)}")


# --- PATCH: robust numeric-tail extractor ---
# Note: order matters. Match percent-like tokens first so "41.10" is not split into "41" and "10".
NUM_TOKEN_RE = re.compile(r"(?<!\w)(\d{1,3}\.\d{2}|-?\d{1,3}(?:,\d{3})+|-?\d+)(?!\w)")

def extract_numeric_tail(all_txt: str) -> Dict[str, Any]:
    """Extract the right-side numeric block by taking the last 6-7 numeric-like tokens.

    Returns a dict with keys:
      shares_before, combined_before, pct_before,
      shares_after, combined_after, pct_after,
      delta_shares_or_delta_field
    """
    out: Dict[str, Any] = {}
    s = _compact_spaced_text(all_txt or "")
    toks = NUM_TOKEN_RE.findall(s)
    if len(toks) < 6:
        return out

    tail = toks[-7:] if len(toks) >= 7 else toks[-6:]
    # If only 6, we treat delta as missing.
    if len(tail) == 6:
        tail = [""] + tail  # pad at front for shares_before

    # Map: shares_before, combined_before, pct_before, shares_after, combined_after, pct_after, delta
    sb, cb, pb, sa, ca, pa, d = tail

    def _pi(x: str):
        return parse_int_token(x)

    def _pp(x: str):
        return parse_pct_token(x)

    out["shares_before"] = _pi(sb)
    out["combined_before"] = _pi(cb)
    out["pct_before"] = _pp(pb)
    out["shares_after"] = _pi(sa)
    out["combined_after"] = _pi(ca)
    out["pct_after"] = _pp(pa)
    out["delta_shares_or_delta_field"] = _norm(d)
    return out


def looks_like_new_record(bucket_text: Dict[str, str]) -> bool:
    """Heuristic: decide if a clustered row begins a new logical record."""
    row_no = bucket_text.get("row_no", "")
    ticker = bucket_text.get("ticker", "")
    status = bucket_text.get("status", "")
    right = " ".join(
        bucket_text.get(k, "")
        for k in ("shares_before", "combined_before", "pct_before", "shares_after", "combined_after", "pct_after", "delta")
    )
    right = _compact_spaced_text(right)

    has_rowno = bool(ROWNO_RE.match(row_no))
    has_ticker = bool(TICKER_RE.search(ticker))
    has_numeric = bool(INT_THOUSANDS_RE.search(right) or PCT_RE.search(right))
    has_status = status.strip().upper() in {"L", "A"}

    # If row has a row number => new record.
    if has_rowno and has_ticker:
        return True

    # If no row number: consider new record if it still has ticker and (numeric or status).
    if has_ticker and (has_numeric or has_status):
        return True

    # Some rows start with ticker and a country word but numeric glued weirdly.
    combined = " ".join(bucket_text.values()).upper()
    if has_ticker and any(c in combined for c in COUNTRY_HINTS) and (has_numeric or has_status):
        return True

    return False


def looks_like_continuation(bucket_text: Dict[str, str], prev_ticker: str) -> bool:
    """Heuristic: row is continuation of previous row (address wrap)."""
    row_no = bucket_text.get("row_no", "")
    status = bucket_text.get("status", "")
    right = " ".join(
        bucket_text.get(k, "")
        for k in ("shares_before", "combined_before", "pct_before", "shares_after", "combined_after", "pct_after", "delta")
    )
    right = _compact_spaced_text(right)
    has_rowno = bool(ROWNO_RE.match(row_no))
    has_numeric = bool(INT_THOUSANDS_RE.search(right) or PCT_RE.search(right))
    has_status = status.strip().upper() in {"L", "A"}

    # If there is no rowno, no numeric and no status => very likely continuation.
    if (not has_rowno) and (not has_numeric) and (not has_status):
        return True

    # If ticker repeats and right-side numeric is empty => continuation.
    ticker = bucket_text.get("ticker", "").strip().upper()
    if ticker and prev_ticker and ticker == prev_ticker and (not has_numeric):
        return True

    return False


def extract_table_from_page(
    page: pdfplumber.page.Page,
    bands: ColumnBands,
    page_no: int,
    source_file: str,
    debug_words_dir: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (raw_row_debug, parsed_records) for a page."""
    # Crop to the table body to speed up extraction & avoid pulling margin/footer noise.
    # Coordinates are in PDF points; (0,0) is top-left for pdfplumber.
    crop_bbox = (15, 55, page.width - 15, page.height - 18)
    cropped = page.crop(crop_bbox)

    # `use_text_flow=True` is slower and can over-merge; positional bucketing does not need it.
    words = cropped.extract_words(use_text_flow=False, keep_blank_chars=False)
    words = dedupe_words(words)

    # `cropped` already removes most header; keep everything from crop.
    data_words = words

    # Estimate tolerance based on median word height.
    heights = [float(w.get("bottom", w["top"])) - float(w["top"]) for w in data_words[:500] if w.get("bottom")]
    med_h = sorted(heights)[len(heights) // 2] if heights else 6.0
    y_tol = max(1.8, min(3.2, med_h * 0.45))

    rows = cluster_rows(data_words, y_tol=y_tol)

    if debug_words_dir:
        debug_words_dir.mkdir(parents=True, exist_ok=True)
        dfw = pd.DataFrame([
            {
                "page": page_no,
                "text": w.get("text"),
                "x0": w.get("x0"),
                "x1": w.get("x1"),
                "top": w.get("top"),
                "bottom": w.get("bottom"),
            }
            for w in data_words
        ])
        dfw.to_csv(debug_words_dir / f"words_p{page_no:03d}.csv", index=False)

    raw_rows_debug: List[Dict[str, Any]] = []
    parsed: List[Dict[str, Any]] = []

    current: Optional[Dict[str, Any]] = None
    prev_ticker = ""

    for ridx, row_words in enumerate(rows):
        # bucketize
        buckets: Dict[str, List[Dict[str, Any]]] = {name: [] for name, _ in bands.cuts}
        for w in row_words:
            b = bands.bucket(_word_mid_x(w), page.width)
            buckets.setdefault(b, []).append(w)

        bucket_text = {k: join_bucket(v) for k, v in buckets.items()}
        # Fix nat/domicile/status using x-positions (handles merged/graphical headers)
        _refine_nat_dom_status_from_positions(row_words, page.width, bucket_text)

        # Build a raw row string for debugging.
        all_txt = join_bucket(row_words)
        all_txt = _compact_spaced_text(all_txt)
        raw_rows_debug.append(
            {
                "source_file": source_file,
                "page": page_no,
                "row_index": ridx,
                "raw_text": all_txt,
                **{f"col_{k}": bucket_text.get(k, "") for k, _ in bands.cuts},
            }
        )

        # Skip obvious header repeats / empty rows
        if not all_txt or "NOKODE" in all_txt.replace(" ", "").upper():
            continue

        # Determine ticker candidate
        t = bucket_text.get("ticker", "")
        # handle glued like "1871YULE" in ticker col or even in row_no col
        if not TICKER_RE.search(t):
            glued = (bucket_text.get("row_no", "") + " " + t).upper()
            m = TICKER_RE.search(glued)
            if m:
                t = m.group(1)
        else:
            t = TICKER_RE.search(t).group(1)

        bucket_text["ticker"] = t

        if current and looks_like_continuation(bucket_text, prev_ticker=prev_ticker):
            # Append to address2 first if present, else address.
            cont = " ".join(
                s for s in [bucket_text.get("address", ""), bucket_text.get("address2", ""), bucket_text.get("emiten", ""), bucket_text.get("broker", ""), bucket_text.get("shareholder", ""), bucket_text.get("account_name", "")] if s
            ).strip()
            if cont:
                if current.get("address2"):
                    current["address2"] = _norm(current["address2"] + " " + cont)
                elif current.get("address"):
                    current["address"] = _norm(current["address"] + " " + cont)
                else:
                    current["address"] = _norm(cont)
                current["raw_text"] = _norm(current.get("raw_text", "") + " | " + all_txt)
                current["parse_warnings"] = _norm((current.get("parse_warnings", "") + " continuation").strip())
            continue

        is_new = looks_like_new_record(bucket_text)
        if not is_new:
            # Not continuation and not new record: ignore (often footers)
            continue

        # Finalize previous
        if current:
            parsed.append(current)

        # Create new record from buckets
        row_no = bucket_text.get("row_no", "")
        m = ROWNO_RE.match(row_no)
        row_no_val = m.group(1) if m else ""

        status = bucket_text.get("status", "").strip().upper()
        if status not in {"L", "A"}:
            status = ""

        # Parse numeric by x-bands (already bucketed)
        shares_before = parse_int_token(bucket_text.get("shares_before", ""))
        combined_before = parse_int_token(bucket_text.get("combined_before", ""))
        pct_before = parse_pct_token(bucket_text.get("pct_before", ""))
        shares_after = parse_int_token(bucket_text.get("shares_after", ""))
        combined_after = parse_int_token(bucket_text.get("combined_after", ""))
        pct_after = parse_pct_token(bucket_text.get("pct_after", ""))

        # Always attempt tail parsing; it's robust when bucketing drifts or glyphs are spaced out.
        tail = extract_numeric_tail(all_txt)

        # Fill missing pct from tail if present
        if pct_before is None and tail.get("pct_before") is not None:
            pct_before = tail.get("pct_before")
        if pct_after is None and tail.get("pct_after") is not None:
            pct_after = tail.get("pct_after")

        # Override suspicious small share numbers (often postal codes / address fragments).
        # Tail shares are typically large for this table.
        def _prefer_tail_int(cur, t):
            if t is None:
                return cur
            if cur is None:
                return t
            if cur < 10000 and t >= 10000:
                return t
            return cur

        shares_before = _prefer_tail_int(shares_before, tail.get("shares_before"))
        combined_before = _prefer_tail_int(combined_before, tail.get("combined_before"))
        shares_after = _prefer_tail_int(shares_after, tail.get("shares_after"))
        combined_after = _prefer_tail_int(combined_after, tail.get("combined_after"))

        # If still missing, take tail
        if shares_before is None and tail.get("shares_before") is not None:
            shares_before = tail.get("shares_before")
        if combined_before is None and tail.get("combined_before") is not None:
            combined_before = tail.get("combined_before")
        if shares_after is None and tail.get("shares_after") is not None:
            shares_after = tail.get("shares_after")
        if combined_after is None and tail.get("combined_after") is not None:
            combined_after = tail.get("combined_after")

        warnings: List[str] = []
        if not t:
            warnings.append("missing_ticker")
        if pct_after is not None and not (0 <= pct_after <= 100):
            warnings.append("pct_after_out_of_range")
        if pct_before is not None and not (0 <= pct_before <= 100):
            warnings.append("pct_before_out_of_range")
        if pct_after is None:
            warnings.append("missing_pct_after")
        if pct_before is None:
            warnings.append("missing_pct_before")

        confidence = 1.0
        # Down-rank if key fields missing
        if not bucket_text.get("broker"):
            confidence -= 0.15
        if not bucket_text.get("shareholder"):
            confidence -= 0.15
        if shares_after is None or shares_before is None:
            confidence -= 0.2
        if pct_after is None or pct_before is None:
            confidence -= 0.2
        confidence = max(0.0, min(1.0, confidence))

        current = {
            "source_file": source_file,
            "page": page_no,
            "row_no": row_no_val,
            "ticker": t,
            "emiten": bucket_text.get("emiten", ""),
            "broker": bucket_text.get("broker", ""),
            "shareholder": bucket_text.get("shareholder", ""),
            "account_name": bucket_text.get("account_name", ""),
            "address": bucket_text.get("address", ""),
            "address2": bucket_text.get("address2", ""),
            "nationality": bucket_text.get("nationality", ""),
            "domicile": bucket_text.get("domicile", ""),
            "status": status,
            "shares_after": shares_after,
            "combined_after": combined_after,
            "pct_after": pct_after,
            "shares_before": shares_before,
            "combined_before": combined_before,
            "pct_before": pct_before,
            "delta_shares_or_delta_field": _norm(bucket_text.get("delta", "")),
            "raw_text": all_txt,
            "parse_warnings": ";".join(warnings),
            "confidence": confidence,
        }

        # If delta is empty, try tail parse (some layouts push delta into numeric tail only)
        if not current.get("delta_shares_or_delta_field"):
            tail = extract_numeric_tail(all_txt)
            if tail.get("delta_shares_or_delta_field"):
                current["delta_shares_or_delta_field"] = tail.get("delta_shares_or_delta_field")

        prev_ticker = t or prev_ticker

    if current:
        parsed.append(current)

    # Merge address+address2
    for r in parsed:
        addr = r.get("address", "")
        addr2 = r.pop("address2", "")
        r["address"] = _norm(" ".join([a for a in [addr, addr2] if a]).strip())

    return raw_rows_debug, parsed


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract KSEI/BEI ownership table into a clean Excel")
    p.add_argument("--pdf", required=True, help="Path to PDF")
    p.add_argument("--out", default="outputs/extracted/ownership_table.xlsx", help="Output Excel path")
    p.add_argument("--debug-dir", default="", help="If set, write debug artifacts to this folder")
    p.add_argument(
        "--include-raw-debug",
        action="store_true",
        help="Also include the raw clustered-row sheet inside the output Excel (can be large/slow).",
    )
    p.add_argument("--max-pages", type=int, default=0, help="If >0, only process first N pages")
    p.add_argument("--page-from", type=int, default=0, help="1-based start page to process (0=auto)")
    p.add_argument("--page-to", type=int, default=0, help="1-based end page to process (0=auto)")
    return p


def main() -> None:
    args = build_argparser().parse_args()
    pdf_path = Path(args.pdf)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None

    raw_debug_all: List[Dict[str, Any]] = []
    rows_all: List[Dict[str, Any]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        # Find first table page (header containing "NoKode")
        table_page_idx = None
        for i, p in enumerate(pdf.pages):
            t = (p.extract_text() or "")
            if "NoKode" in t or "NoKode Efek" in t:
                table_page_idx = i
                break
        if table_page_idx is None:
            raise RuntimeError("Could not locate table header in PDF")

        bands = infer_bands_from_header(pdf.pages[table_page_idx])

        # Print header layout (requested by user)
        print("[header layout] column cutpoints:")
        for name, x0 in bands.cuts:
            print(f"  {name:16s} x0={x0:.1f}")

        # Determine processing window
        max_pages = args.max_pages if args.max_pages and args.max_pages > 0 else len(pdf.pages)
        auto_start = table_page_idx + 1
        start_page = args.page_from if args.page_from and args.page_from > 0 else auto_start
        end_page = args.page_to if args.page_to and args.page_to > 0 else min(len(pdf.pages), max_pages)

        start_idx = max(0, start_page - 1)
        end_idx_excl = min(len(pdf.pages), end_page)

        for pidx in range(start_idx, end_idx_excl):
            if (pidx - table_page_idx) % 5 == 0:
                print(f"[progress] processing page {pidx+1}/{end_page}")
            page = pdf.pages[pidx]
            raw_dbg, parsed = extract_table_from_page(
                page,
                bands=bands,
                page_no=pidx + 1,
                source_file=str(pdf_path.name),
                debug_words_dir=(debug_dir / "words") if debug_dir else None,
            )
            if args.include_raw_debug:
                raw_debug_all.extend(raw_dbg)
            rows_all.extend(parsed)

    print(f"[progress] finished page parsing, total parsed rows so far: {len(rows_all)}")

    # Post validation / type coercion
    df = pd.DataFrame(rows_all)
    # Ensure stable schema
    wanted = [
        "source_file",
        "page",
        "row_no",
        "ticker",
        "emiten",
        "broker",
        "shareholder",
        "account_name",
        "address",
        "nationality",
        "domicile",
        "status",
        "shares_after",
        "combined_after",
        "pct_after",
        "shares_before",
        "combined_before",
        "pct_before",
        "delta_shares_or_delta_field",
        "raw_text",
        "parse_warnings",
        "confidence",
    ]
    for c in wanted:
        if c not in df.columns:
            df[c] = "" if c in {"raw_text", "parse_warnings"} else None
    df = df[wanted]

    # Coerce numeric columns
    for col in ["page", "shares_after", "combined_after", "shares_before", "combined_before"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in ["pct_after", "pct_before", "confidence"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Quality checks printed to stdout
    n_rows = len(df)
    pct_bad = int(((df["pct_after"].notna()) & ((df["pct_after"] < 0) | (df["pct_after"] > 100))).sum())
    pct_bad2 = int(((df["pct_before"].notna()) & ((df["pct_before"] < 0) | (df["pct_before"] > 100))).sum())
    print(f"[summary] extracted rows: {n_rows}")
    print(f"[summary] pct_after out-of-range: {pct_bad} | pct_before out-of-range: {pct_bad2}")

    # Write Excel
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="ownership_table")
        if raw_debug_all:
            pd.DataFrame(raw_debug_all).to_excel(xw, index=False, sheet_name="raw_rows_debug")

    print(f"[done] wrote: {out_path}")


if __name__ == "__main__":
    main()
