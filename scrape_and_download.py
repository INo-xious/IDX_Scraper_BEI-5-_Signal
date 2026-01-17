import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from tqdm import tqdm
from playwright.async_api import async_playwright

IDX_URL = "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi"
DEFAULT_KEYWORD = "5%"


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape IDX KI attachments and optionally extract ownership tables to Excel"
    )
    p.add_argument(
        "--keyword",
        default=DEFAULT_KEYWORD,
        help='Search keyword typed into IDX "Kata kunci" (default: 5%%)',
    )
    p.add_argument(
        "--out-pdf-dir",
        default="outputs/pdfs",
        help="Folder to save downloaded PDFs (default: outputs/pdfs)",
    )
    p.add_argument(
        "--max-pdfs",
        type=int,
        default=0,
        help="If >0, stop after downloading this many PDFs (useful for testing)",
    )
    p.add_argument(
        "--extract",
        action="store_true",
        help="After download, run extract_ownership_table.py on each PDF",
    )
    p.add_argument(
        "--extract-out-dir",
        default="outputs/extracted",
        help="Where to save extracted XLSX files (default: outputs/extracted)",
    )
    p.add_argument(
        "--extract-debug-dir",
        default="",
        help="If set, write debug artifacts per PDF under this folder",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download / re-extract even if output files already exist",
    )
    return p


def run_extractor(pdf_path: Path, out_xlsx: Path, debug_root: str = "") -> None:
    """Call the robust ownership extractor as a subprocess.

    We intentionally use subprocess to avoid import/path issues when users run from
    different working directories.
    """
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("extract_ownership_table.py")),
        "--pdf",
        str(pdf_path),
        "--out",
        str(out_xlsx),
    ]
    if debug_root:
        debug_dir = Path(debug_root) / pdf_path.stem
        cmd += ["--debug-dir", str(debug_dir)]
    # Show extractor output live
    subprocess.run(cmd, check=True)

PDF_RE = re.compile(r"\.pdf(\?|$)", re.IGNORECASE)

def safe_filename(url: str, preferred_name: str | None = None) -> str:
    # Prefer the visible link text (usually the real filename)
    name = (preferred_name or "").strip()

    if not name:
        path = urllib.parse.urlparse(url).path
        name = os.path.basename(path) or "document.pdf"

    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    # sanitize
    name = re.sub(r"[^\w\-. ()]", "_", name)[:180]
    return name

def looks_like_pdf(data: bytes) -> bool:
    # Real PDFs start with '%PDF' header
    if not data or len(data) < 1024:
        return False
    return data[:4] == b"%PDF"

async def main(args: argparse.Namespace):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # ubah False kalau mau lihat
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto(IDX_URL, wait_until="networkidle")

        # Pastikan fokus di halaman yang benar
        # (Kalau tab/section berubah, ini aman)
        ki_tab = await page.query_selector('a:has-text("Keterbukaan Informasi")')
        if ki_tab:
            await ki_tab.click()
            await page.wait_for_timeout(1000)

        # Input "Kata kunci..." sesuai screenshot kamu
        search_box = await page.query_selector('input[placeholder*="Kata kunci" i]')
        if not search_box:
            raise RuntimeError('Input "Kata kunci..." tidak ditemukan. Cek selector halaman IDX.')

        await search_box.click()
        await search_box.fill(args.keyword)
        await search_box.press("Enter")
        await page.wait_for_timeout(2000)

        pdf_links: dict[str, str] = {}

        async def collect_pdf_links():
            anchors = await page.query_selector_all("a[href]")
            for a in anchors:
                href = await a.get_attribute("href")
                if not href:
                    continue

                # Robust visible text extraction + normalize whitespace
                txt = (await a.text_content()) or ""
                txt = re.sub(r"\s+", " ", txt).strip()
                txt_l = txt.lower()

                # Must look like a filename (ends with .pdf)
                if not txt_l.endswith(".pdf"):
                    continue

                # Must look like IDX attachment filename, e.g. starts with YYYYMMDD_...
                # Some useful files may not contain 'lamp', so we accept either:
                # - an 8-digit date prefix anywhere, OR
                # - '_lamp' token
                looks_like_idx_filename = bool(re.search(r"\b20\d{6}\b", txt)) or ("_lamp" in txt_l)
                if not looks_like_idx_filename:
                    continue

                full = urllib.parse.urljoin(page.url, href)
                if PDF_RE.search(full):
                    # keep the first seen preferred filename for this URL
                    pdf_links.setdefault(full, txt)

        # Pagination loop yang lebih aman:
        # cari tombol Next/Berikutnya, stop kalau disabled / tidak ada
        while True:
            await collect_pdf_links()

            next_selectors = [
                'a[aria-label="Next"]',
                'button[aria-label="Next"]',
                'a:has-text("Berikutnya")',
                'button:has-text("Berikutnya")',
                'a:has-text("Next")',
                'button:has-text("Next")',
                'li.pagination-next a',
            ]
            next_btn = None
            for sel in next_selectors:
                el = await page.query_selector(sel)
                if el:
                    next_btn = el
                    break

            if not next_btn:
                break

            aria_disabled = (await next_btn.get_attribute("aria-disabled")) or ""
            disabled_attr = await next_btn.get_attribute("disabled")
            cls = (await next_btn.get_attribute("class")) or ""

            if disabled_attr is not None or aria_disabled.lower() == "true" or "disabled" in cls.lower():
                break

            await next_btn.click()
            await page.wait_for_timeout(1500)

        print(f"Found {len(pdf_links)} PDF links")

        # Build requests session with cookies from Playwright (mengurangi 403)
        cookies = await context.cookies()
        jar = requests.cookies.RequestsCookieJar()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

        sess = requests.Session()
        sess.cookies = jar
        sess.headers.update({"User-Agent": await page.evaluate("() => navigator.userAgent")})

        out_dir = Path(args.out_pdf_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        downloaded: list[Path] = []
        # Download PDFs
        for idx, (url, preferred_name) in enumerate(
            tqdm(sorted(pdf_links.items()), desc="Downloading PDFs")
        ):
            if args.max_pdfs and idx >= args.max_pdfs:
                break

            out_path = out_dir / safe_filename(url, preferred_name)
            if (
                (not args.overwrite)
                and out_path.exists()
                and out_path.stat().st_size > 0
            ):
                downloaded.append(out_path)
                continue

            # polite delay (optional)
            time.sleep(0.2)

            r = sess.get(url, timeout=90)
            r.raise_for_status()
            data = r.content

            # Skip links that are not real PDFs (some IDX links return HTML/empty)
            if not looks_like_pdf(data):
                print(f"[SKIP] Not a real PDF (or too small): {url}")
                continue

            out_path.write_bytes(data)
            downloaded.append(out_path)

        await browser.close()
        print(f"Done. PDFs saved to: {out_dir.resolve()}")

        # Optional extraction stage
        if args.extract and downloaded:
            extract_out_dir = Path(args.extract_out_dir)
            extract_out_dir.mkdir(parents=True, exist_ok=True)
            print(f"[extract] Running ownership extractor on {len(downloaded)} PDFs")

            for pdf_path in tqdm(downloaded, desc="Extracting to XLSX"):
                out_xlsx = extract_out_dir / f"{pdf_path.stem}.ownership_table.xlsx"
                if out_xlsx.exists() and (not args.overwrite):
                    continue
                try:
                    run_extractor(pdf_path, out_xlsx, debug_root=args.extract_debug_dir)
                except subprocess.CalledProcessError as e:
                    # Don't stop the entire pipeline if one PDF is malformed.
                    print(f"[extract][ERROR] {pdf_path.name}: {e}")

if __name__ == "__main__":
    parsed = build_argparser().parse_args()
    asyncio.run(main(parsed))