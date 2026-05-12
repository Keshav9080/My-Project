from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent
DATA_CSV = ROOT / "data.csv"
DRHP_CSV = ROOT / "drhp_companies_specified_format.csv"
DRHP_LINKS_CSV = ROOT / "drhp_rows_with_links.csv"
DRHP_CHECKPOINT = ROOT / "drhp_enrichment_checkpoint.json"

DSIR_PDF_URL = "https://www.dsir.gov.in/sites/default/files/2024-09/24_rdidir_0.pdf"
DSIR_PDF = ROOT / "dsir_directory_2024_07_31.pdf"
DSIR_TEXT = ROOT / "dsir_directory_2024_07_31.txt"
PROMOTER_CHECKPOINT = ROOT / "promoter_extraction_checkpoint.json"

DATA_DSIR_OUT = ROOT / "data_with_dsir_recognition.csv"
FINAL_OUT = ROOT / "final_merged_dsir_promoters.csv"

CURL_BIN = shutil.which("curl.exe") or shutil.which("curl")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def normalize_name(value: str) -> str:
    value = value.upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    value = re.sub(
        r"\b(PRIVATE|PVT|PUBLIC|LIMITED|LTD|LLP|INDIA|THE|CO|COMPANY)\b",
        " ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def curl_download(url: str, path: Path, timeout: int = 120) -> bool:
    if not CURL_BIN:
        return False
    try:
        subprocess.run(
            [
                CURL_BIN,
                "-sL",
                "--fail",
                "-m",
                str(timeout),
                "-A",
                USER_AGENT,
                "-o",
                str(path),
                url,
            ],
            check=True,
            capture_output=True,
            timeout=timeout + 10,
        )
        return path.exists() and path.stat().st_size > 1000
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def pdf_text_from_bytes(data: bytes, max_pages: int = 45) -> str:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return ""
    parts: list[str] = []
    for page in doc[: min(len(doc), max_pages)]:
        try:
            parts.append(page.get_text("text"))
        except Exception:
            continue
    return "\n".join(parts)


def ensure_dsir_text() -> str:
    if not DSIR_TEXT.exists():
        if not DSIR_PDF.exists():
            ok = curl_download(DSIR_PDF_URL, DSIR_PDF, timeout=180)
            if not ok:
                agent_tools = (
                    Path.home()
                    / ".cursor"
                    / "projects"
                    / "c-Training-Projects-loan-approval"
                    / "agent-tools"
                )
                for candidate in sorted(agent_tools.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    compact_heading = re.sub(r"\s+", " ", text[:2000]).upper()
                    if "RECOGNISED IN-HOUSE R&D UNITS" in compact_heading:
                        DSIR_TEXT.write_text(text, encoding="utf-8")
                        return text
                raise RuntimeError("Could not download or locate DSIR directory text.")
        text = pdf_text_from_bytes(DSIR_PDF.read_bytes(), max_pages=10_000)
        DSIR_TEXT.write_text(text, encoding="utf-8")
    return DSIR_TEXT.read_text(encoding="utf-8", errors="replace")


def dsir_evidence(company: str, dsir_norm_text: str) -> str:
    norm = normalize_name(company)
    compact = norm.replace(" ", "")
    if len(compact) >= 8 and compact in dsir_norm_text:
        return "Matched in DSIR Directory of Recognized In-House R&D Units, 31.07.2024."
    return "No match found in DSIR Directory of Recognized In-House R&D Units, 31.07.2024."


def add_dsir_to_data() -> list[dict[str, str]]:
    rows = read_csv(DATA_CSV)
    dsir_text = ensure_dsir_text()
    dsir_norm_text = normalize_name(dsir_text).replace(" ", "")
    for row in rows:
        company = row.get("Company Name") or row.get("Company name") or ""
        row["DSIR Recognition Evidence"] = dsir_evidence(company, dsir_norm_text)
    fieldnames = list(rows[0].keys()) if rows else []
    if "DSIR Recognition Evidence" not in fieldnames:
        fieldnames.append("DSIR Recognition Evidence")
    write_csv(DATA_DSIR_OUT, rows, fieldnames)
    return rows


def pdf_bytes_from_url(url: str, idx: str) -> bytes | None:
    cache = ROOT / "_drhp_promoter_cache"
    cache.mkdir(exist_ok=True)
    suffix = ".zip" if url.lower().endswith(".zip") else ".pdf"
    raw_path = cache / f"{idx}{suffix}"
    if not raw_path.exists() or raw_path.stat().st_size < 1000:
        if not curl_download(url, raw_path, timeout=120):
            return None
    data = raw_path.read_bytes()
    if suffix == ".pdf":
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            pdfs = [name for name in zf.namelist() if name.lower().endswith(".pdf")]
            if not pdfs:
                return None
            pdfs.sort(key=lambda name: ("drhp" not in name.lower(), len(name)))
            return zf.read(pdfs[0])
    except zipfile.BadZipFile:
        return None


GENERIC_PROMOTER_VALUES = {
    "",
    "See DRHP 'Promoters' and 'Management' sections.",
    "See DRHP \u2018Promoters\u2019 and \u2018Management\u2019 sections.",
}


def clean_person_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" .,:;|-")
    name = re.sub(r"\b(and|or|the|our|company|promoter|promoters)\b$", "", name, flags=re.I)
    return name.strip(" .,:;|-")


def extract_promoters_from_text(text: str) -> str:
    if not text.strip():
        return "Not extracted from PDF text; verify in IPO prospectus or LinkedIn."

    search_area = text[:90_000]
    snippets: list[str] = []
    for marker in re.finditer(r"\b(promoters?|details of our promoters?|our promoters)\b", search_area, re.I):
        start = max(0, marker.start() - 600)
        end = min(len(search_area), marker.end() + 3500)
        snippets.append(search_area[start:end])

    names: list[str] = []
    for blob in snippets[:10]:
        for match in re.finditer(
            r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?)\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,5})",
            blob,
        ):
            candidate = clean_person_name(match.group(0))
            if 6 <= len(candidate) <= 90 and candidate not in names:
                names.append(candidate)

        direct_patterns = [
            r"(?:Our Promoters are|Promoters of our Company are|The Promoters are)\s+([^.\n]{10,250})",
            r"(?:our promoters, namely,|promoters namely)\s+([^.\n]{10,250})",
        ]
        for pat in direct_patterns:
            found = re.search(pat, blob, re.I)
            if found:
                piece = re.sub(r"\s+", " ", found.group(1)).strip(" .")
                for token in re.split(r",| and |;", piece):
                    candidate = clean_person_name(token)
                    if 6 <= len(candidate) <= 90 and candidate not in names:
                        names.append(candidate)

    if names:
        return "DRHP: " + "; ".join(names[:10])
    return "Promoter names not machine-extracted from sampled DRHP pages; verify in IPO prospectus or LinkedIn."


def load_prior_promoters() -> dict[str, str]:
    prior: dict[str, str] = {}
    if DRHP_CHECKPOINT.exists():
        data = json.loads(DRHP_CHECKPOINT.read_text(encoding="utf-8"))
        for _idx, row in data.get("done", {}).items():
            company = row.get("Company name", "")
            promoter = row.get("Promoter / Decision-Maker", "")
            if promoter not in GENERIC_PROMOTER_VALUES:
                prior[normalize_name(company)] = promoter
    return prior


def load_promoter_checkpoint() -> dict[str, str]:
    if PROMOTER_CHECKPOINT.exists():
        return json.loads(PROMOTER_CHECKPOINT.read_text(encoding="utf-8"))
    return {}


def save_promoter_checkpoint(data: dict[str, str]) -> None:
    PROMOTER_CHECKPOINT.write_text(json.dumps(data, indent=0), encoding="utf-8")


def fill_drhp_promoters() -> list[dict[str, str]]:
    rows = read_csv(DRHP_CSV)
    links = read_csv(DRHP_LINKS_CSV)
    links_by_name: dict[str, list[dict[str, str]]] = {}
    for row in links:
        links_by_name.setdefault(normalize_name(row.get("company_name", "")), []).append(row)

    prior = load_prior_promoters()
    checkpoint = load_promoter_checkpoint()

    def work(row: dict[str, str]) -> tuple[str, str]:
        company = row.get("Company name", "")
        key = normalize_name(company)
        existing = row.get("Promoter / Decision-Maker", "")
        if existing and existing not in GENERIC_PROMOTER_VALUES:
            return key, existing
        if key in checkpoint:
            return key, checkpoint[key]
        if key in prior:
            return key, prior[key]
        link_rows = links_by_name.get(key, [])
        if not link_rows:
            return key, "No BSE DRHP link found for promoter extraction."
        pdf = pdf_bytes_from_url(link_rows[0].get("drhp_or_draft_document_url", ""), link_rows[0].get("row_index", key))
        if not pdf:
            return key, "Could not download DRHP PDF/ZIP for promoter extraction."
        text = pdf_text_from_bytes(pdf, max_pages=55)
        return key, extract_promoters_from_text(text)

    unique_rows: dict[str, dict[str, str]] = {}
    for row in rows:
        unique_rows.setdefault(normalize_name(row.get("Company name", "")), row)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(work, row) for row in unique_rows.values()]
        for i, fut in enumerate(as_completed(futures), 1):
            key, value = fut.result()
            checkpoint[key] = value
            if i % 10 == 0:
                save_promoter_checkpoint(checkpoint)
                print(f"promoter checkpoint {i}/{len(futures)}", flush=True)

    save_promoter_checkpoint(checkpoint)
    for row in rows:
        key = normalize_name(row.get("Company name", ""))
        row["Promoter / Decision-Maker"] = checkpoint.get(key, row.get("Promoter / Decision-Maker", ""))

    write_csv(DRHP_CSV, rows, list(rows[0].keys()))
    return rows


def merge_final(data_rows: list[dict[str, str]], drhp_rows: list[dict[str, str]]) -> None:
    data_by_key = {normalize_name(row.get("Company Name", "")): row for row in data_rows}
    drhp_by_key = {normalize_name(row.get("Company name", "")): row for row in drhp_rows}
    keys = sorted(set(data_by_key) | set(drhp_by_key))

    final_fields = [
        "Company Name",
        "City/Location",
        "Industry Segment",
        "Website",
        "Revenue Band",
        "DSIR Recognition Evidence",
        "Promoter/Decision-Maker",
        "Growth Signals",
        "Source",
    ]
    out: list[dict[str, str]] = []
    for key in keys:
        data = data_by_key.get(key, {})
        drhp = drhp_by_key.get(key, {})
        out.append(
            {
                "Company Name": data.get("Company Name") or drhp.get("Company name", ""),
                "City/Location": data.get("City/Location") or drhp.get("City / Location", ""),
                "Industry Segment": data.get("Industry Segment") or drhp.get("Industry Segment", ""),
                "Website": data.get("Website") or drhp.get("Website", ""),
                "Revenue Band": data.get("Revenue Band") or drhp.get("Revenue Band", ""),
                "DSIR Recognition Evidence": data.get("DSIR Recognition Evidence")
                or drhp.get("DSIR Recognition Evidence", ""),
                "Promoter/Decision-Maker": drhp.get("Promoter / Decision-Maker")
                or data.get("Promoter/Decision-Maker", ""),
                "Growth Signals": data.get("Growth Signals") or drhp.get("Growth Signals", ""),
                "Source": "data.csv + BSE DRHP" if data and drhp else ("data.csv" if data else "BSE DRHP"),
            }
        )

    write_csv(FINAL_OUT, out, final_fields)


def main() -> None:
    t0 = time.time()
    data_rows = add_dsir_to_data()
    print(f"Wrote {DATA_DSIR_OUT.name} with DSIR evidence for {len(data_rows)} rows.", flush=True)
    drhp_rows = fill_drhp_promoters()
    print(f"Updated {DRHP_CSV.name} promoter column for {len(drhp_rows)} rows.", flush=True)
    merge_final(data_rows, drhp_rows)
    print(f"Wrote {FINAL_OUT.name} in {time.time() - t0:.1f}s.", flush=True)


if __name__ == "__main__":
    main()
