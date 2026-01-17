import subprocess
import sys
import shutil
import os
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent

SCRAPE_SCRIPT = ROOT / "scrape_and_download.py"
# Robust extractor for the black multi-column ownership table
EXTRACT_SCRIPT = ROOT / "extract_ownership_table.py"

PDF_DIR = ROOT / "outputs" / "pdfs"
EXTRACTED_DIR = ROOT / "outputs" / "extracted"
FINAL_FILE = EXTRACTED_DIR / "ownership_table.xlsx"

st.set_page_config(page_title="IDX KI Pipeline", layout="wide")

st.title("IDX/KSEI — Ownership Table Extractor (5%+)")
st.caption("Run: Scrape → Batch Extract → Merge to Master Excel")

with st.sidebar:
    st.header("Settings")
    keyword = st.text_input("IDX keyword", value="5%")
    max_pdfs = st.number_input("Max PDFs (0 = all)", min_value=0, value=0, step=1)
    do_merge = st.checkbox("Merge all ownership tables into one master Excel", value=True)
    do_cleanup = st.checkbox("Cleanup intermediate files after run", value=False)
    do_open_folder = st.checkbox("Auto-open output folder after run", value=True)


col1, col2, col3 = st.columns([1, 1, 2])

def run_script(script_path: Path, args: list[str] | None = None):
    if not script_path.exists():
        raise FileNotFoundError(f"Missing script: {script_path.name}")

    st.write(f"### ▶ Running: {script_path.name}")
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd += args
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        st.write(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"{script_path.name} failed (exit code {rc})")


def open_folder_in_os(folder: Path) -> None:
    """Best-effort open folder in OS file explorer."""
    try:
        folder = folder.resolve()
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        # Streamlit runs in different environments; opening may fail.
        pass


def extract_one_pdf(pdf_path: Path, out_xlsx: Path) -> tuple[bool, str]:
    """Run extractor for one PDF. Returns (ok, log_tail)."""
    cmd = [
        sys.executable,
        str(EXTRACT_SCRIPT),
        "--pdf",
        str(pdf_path),
        "--out",
        str(out_xlsx),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
        tail = (out.stdout or "")[-2000:]
        return True, tail
    except subprocess.CalledProcessError as e:
        msg = (e.stdout or "") + "\n" + (e.stderr or "")
        return False, msg[-4000:]


def merge_excels(excel_files: list[Path], out_file: Path) -> tuple[int, int]:
    """Merge multiple ownership-table excels into one.

    Returns: (file_count, row_count)
    """
    frames = []
    for f in excel_files:
        try:
            df = pd.read_excel(f)
            df["_source_xlsx"] = f.name
            frames.append(df)
        except Exception:
            continue
    if not frames:
        raise RuntimeError("No readable Excel files to merge.")
    merged = pd.concat(frames, ignore_index=True)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    merged.to_excel(out_file, index=False)
    return len(frames), int(len(merged))

def cleanup():
    st.write("### 🧹 Cleaning up intermediate files...")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

    # clear pdfs
    if PDF_DIR.exists():
        for f in PDF_DIR.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
                else:
                    shutil.rmtree(f)
            except Exception:
                pass

    # clear extracted except master
    if EXTRACTED_DIR.exists():
        for f in EXTRACTED_DIR.glob("*.xlsx"):
            if f.name != FINAL_FILE.name:
                try:
                    f.unlink()
                except Exception:
                    pass

    st.success("Cleanup done. Kept ownership_table.xlsx only.")

with col1:
    run_clicked = st.button("Run Pipeline", type="primary")

with col2:
    open_folder = st.button("Show Output Folder Path")

with col3:
    st.info(f"Final output: {FINAL_FILE}")

if open_folder:
    st.write(f"Output folder: `{EXTRACTED_DIR.resolve()}`")
    if st.button("Open output folder in OS"):
        open_folder_in_os(EXTRACTED_DIR)

if run_clicked:
    try:
        EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
        PDF_DIR.mkdir(parents=True, exist_ok=True)

        # 1) Scrape + download PDFs
        scrape_args = [
            "--keyword",
            str(keyword),
            "--out-pdf-dir",
            str(PDF_DIR),
        ]
        if int(max_pdfs) > 0:
            scrape_args += ["--max-pdfs", str(int(max_pdfs))]
        run_script(SCRAPE_SCRIPT, scrape_args)

        # 2) Batch extract all PDFs
        pdfs = sorted(PDF_DIR.glob("*.pdf"))
        if not pdfs:
            raise RuntimeError(f"No PDFs found in {PDF_DIR}")

        st.write("### 📦 Batch extracting PDFs...")
        prog = st.progress(0.0)
        status_box = st.empty()
        log_box = st.expander("Extractor logs (tail)", expanded=False)

        ok_count = 0
        fail_count = 0
        per_pdf_outputs: list[Path] = []

        for i, pdf in enumerate(pdfs, start=1):
            out_xlsx = EXTRACTED_DIR / f"{pdf.stem}.ownership_table.xlsx"
            status_box.info(f"[{i}/{len(pdfs)}] Extracting: {pdf.name}")
            ok, tail = extract_one_pdf(pdf, out_xlsx)
            with log_box:
                st.text(f"--- {pdf.name} ---\n{tail}\n")
            if ok and out_xlsx.exists():
                ok_count += 1
                per_pdf_outputs.append(out_xlsx)
            else:
                fail_count += 1
            prog.progress(i / len(pdfs))

        st.write(f"✅ Extracted: {ok_count} | ❌ Failed: {fail_count}")

        # 3) Merge
        if do_merge:
            st.write("### 🧩 Merging all ownership tables...")
            file_count, row_count = merge_excels(per_pdf_outputs, FINAL_FILE)
            st.success(f"Merged {file_count} files → {row_count:,} rows → {FINAL_FILE.name}")
        else:
            st.info("Merge disabled — per-PDF Excel files are kept in outputs/extracted")

        # 4) Cleanup
        if do_cleanup:
            cleanup()

        # 5) Auto-open folder
        if do_open_folder:
            open_folder_in_os(EXTRACTED_DIR)

        # Download buttons
        if FINAL_FILE.exists():
            st.success("🎉 DONE!")
            with open(FINAL_FILE, "rb") as f:
                st.download_button(
                    "Download ownership_table.xlsx (master)",
                    data=f,
                    file_name="ownership_table.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

        if per_pdf_outputs:
            st.write("### 📄 Per-PDF outputs")
            st.write("(Stored in outputs/extracted/*.ownership_table.xlsx)")
    except Exception as e:
        st.error(f"ERROR: {e}")