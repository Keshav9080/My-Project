"""
Three-phase finalizer:
  Phase 1  – Merge data_with_dsir_recognition.csv + drhp_companies_specified_format.csv
             using whatever promoter data is already in the two checkpoints.
  Phase 2  – For remaining DRHP companies with no promoter yet, download their DRHP PDF
             (cached) and extract promoter names; update the merged file every 10 rows.
  Phase 3  – Write final_merged_dsir_promoters.csv (final output).

Resume-safe: already-processed rows are skipped via promoter_extraction_checkpoint.json.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent
DATA_CSV        = ROOT / "data_with_dsir_recognition.csv"   # already enriched
DRHP_CSV        = ROOT / "drhp_companies_specified_format.csv"
DRHP_LINKS_CSV  = ROOT / "drhp_rows_with_links.csv"
ENRICH_CP       = ROOT / "drhp_enrichment_checkpoint.json"
PROMOTER_CP     = ROOT / "promoter_extraction_checkpoint.json"
CACHE_DIR       = ROOT / "_drhp_promoter_cache"
FINAL_OUT       = ROOT / "final_merged_dsir_promoters.csv"

CURL_BIN   = shutil.which("curl.exe") or shutil.which("curl")
UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DL_TIMEOUT = 90
MAX_PAGES  = 55
WORKERS    = 6

GENERIC_VALUES = {
    "",
    "See DRHP 'Promoters' and 'Management' sections.",
    "See DRHP \u2018Promoters\u2019 and \u2018Management\u2019 sections.",
    "No BSE DRHP link found for promoter extraction.",
    "Could not download DRHP PDF/ZIP for promoter extraction.",
    "Not extracted from PDF text; verify in IPO prospectus or LinkedIn.",
    "Promoter names not machine-extracted from sampled DRHP pages; verify in IPO prospectus or LinkedIn.",
}

FINAL_FIELDS = [
    "Company Name",
    "City / Location",
    "Industry Segment",
    "Website",
    "Revenue Band",
    "DSIR Recognition Evidence",
    "Promoter / Decision-Maker",
    "Growth Signals",
    "Source",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def norm(v: str) -> str:
    v = v.upper().replace("&", " AND ")
    v = re.sub(r"[^A-Z0-9 ]+", " ", v)
    v = re.sub(r"\b(PRIVATE|PVT|PUBLIC|LIMITED|LTD|LLP|INDIA|THE|CO|COMPANY)\b", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def read_csv(p: Path) -> list[dict[str, str]]:
    with p.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: row.get(k, "") for k in fields} for row in rows)


def load_promoter_cache() -> dict[str, str]:
    """Merge enrichment checkpoint + promoter checkpoint, return norm_name → promoter."""
    result: dict[str, str] = {}
    if ENRICH_CP.exists():
        for row in json.loads(ENRICH_CP.read_text(encoding="utf-8")).get("done", {}).values():
            p = row.get("Promoter / Decision-Maker", "")
            if p not in GENERIC_VALUES:
                result[norm(row.get("Company name", ""))] = p
    if PROMOTER_CP.exists():
        for k, v in json.loads(PROMOTER_CP.read_text(encoding="utf-8")).items():
            if v not in GENERIC_VALUES:
                result[k] = v
            elif k not in result:
                result[k] = v
    return result


def save_promoter_cache(data: dict[str, str]) -> None:
    PROMOTER_CP.write_text(json.dumps(data, indent=0), encoding="utf-8")


# ── PDF download / parse ──────────────────────────────────────────────────────

def curl_get(url: str, out: Path) -> bool:
    if not CURL_BIN:
        return False
    fd, tmp = tempfile.mkstemp(suffix=out.suffix, dir=str(out.parent))
    os.close(fd)
    try:
        subprocess.run(
            [CURL_BIN, "-sL", "--fail", "-m", str(DL_TIMEOUT), "-A", UA, "-o", tmp, url],
            check=True, capture_output=True, timeout=DL_TIMEOUT + 10,
        )
        Path(tmp).replace(out)
        return out.exists() and out.stat().st_size > 500
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        Path(tmp).unlink(missing_ok=True)
        return False


def get_pdf_bytes(url: str, key: str) -> bytes | None:
    CACHE_DIR.mkdir(exist_ok=True)
    suffix = ".zip" if url.lower().endswith(".zip") else ".pdf"
    cached = CACHE_DIR / (re.sub(r"[^a-z0-9]", "_", key.lower())[:60] + suffix)
    if not (cached.exists() and cached.stat().st_size > 500):
        if not curl_get(url, cached):
            return None
    data = cached.read_bytes()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                pdfs = sorted(
                    [n for n in zf.namelist() if n.lower().endswith(".pdf")],
                    key=lambda n: ("drhp" not in n.lower(), len(n)),
                )
                return zf.read(pdfs[0]) if pdfs else None
        except zipfile.BadZipFile:
            return None
    return data


def extract_text(pdf_bytes: bytes) -> str:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = []
        for page in doc[: min(len(doc), MAX_PAGES)]:
            try:
                parts.append(page.get_text("text"))
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""


def extract_promoters(text: str) -> str:
    if not text.strip():
        return "PDF had no extractable text (scanned) — verify in IPO prospectus."
    blob = text[:90_000]
    snippets: list[str] = []
    for m in re.finditer(r"\b(promoters?|details of our promoters?|our promoters)\b", blob, re.I):
        snippets.append(blob[max(0, m.start() - 400): m.end() + 3500])

    names: list[str] = []
    for snippet in snippets[:10]:
        for m in re.finditer(
            r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?)\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,5})",
            snippet,
        ):
            cand = re.sub(r"\s+", " ", m.group(0)).strip(" .,:;|-")
            if 6 <= len(cand) <= 90 and cand not in names:
                names.append(cand)
        for pat in (
            r"(?:Our Promoters are|Promoters of our Company are|The Promoters are)\s+([^.\n]{10,250})",
            r"(?:our promoters,?\s+namely,?|promoters namely)\s+([^.\n]{10,250})",
        ):
            found = re.search(pat, snippet, re.I)
            if found:
                for token in re.split(r",| and |;", found.group(1)):
                    cand = re.sub(r"\s+", " ", token).strip(" .,:;|-")
                    if 6 <= len(cand) <= 90 and cand not in names:
                        names.append(cand)

    if names:
        return "DRHP: " + "; ".join(names[:10])
    return "Promoter names not machine-extracted — verify in IPO prospectus or LinkedIn."


# ── phase 1: assemble merged rows using cached data ───────────────────────────

def phase1_build_merged() -> list[dict[str, str]]:
    promoter_cache = load_promoter_cache()
    data_rows  = read_csv(DATA_CSV)  if DATA_CSV.exists()  else []
    drhp_rows  = read_csv(DRHP_CSV)  if DRHP_CSV.exists()  else []

    # index data.csv by normalised company name
    data_idx: dict[str, dict[str, str]] = {}
    for row in data_rows:
        data_idx[norm(row.get("Company Name", "") or row.get("Company name", ""))] = row

    # build merged rows from DRHP list (primary source for IPO companies)
    seen: set[str] = set()
    merged: list[dict[str, str]] = []
    for row in drhp_rows:
        key = norm(row.get("Company name", ""))
        if key in seen:
            continue
        seen.add(key)
        data = data_idx.get(key, {})
        promoter = promoter_cache.get(key, row.get("Promoter / Decision-Maker", ""))
        if promoter in GENERIC_VALUES:
            promoter = data.get("Promoter/Decision-Maker", "") or promoter
        merged.append({
            "Company Name":            row.get("Company name", ""),
            "City / Location":         row.get("City / Location") or data.get("City/Location", ""),
            "Industry Segment":        row.get("Industry Segment") or data.get("Industry Segment", ""),
            "Website":                 row.get("Website") or data.get("Website", ""),
            "Revenue Band":            row.get("Revenue Band") or data.get("Revenue Band", ""),
            "DSIR Recognition Evidence": row.get("DSIR Recognition Evidence")
                                          or data.get("DSIR Recognition Evidence", ""),
            "Promoter / Decision-Maker": promoter,
            "Growth Signals":          row.get("Growth Signals") or data.get("Growth Signals", ""),
            "Source":                  "BSE DRHP" + (" + data.csv" if data else ""),
        })

    # add data.csv rows that are NOT already in DRHP list
    for row in data_rows:
        key = norm(row.get("Company Name", "") or row.get("Company name", ""))
        if key not in seen:
            seen.add(key)
            merged.append({
                "Company Name":              row.get("Company Name", ""),
                "City / Location":           row.get("City/Location", ""),
                "Industry Segment":          row.get("Industry Segment", ""),
                "Website":                   row.get("Website", ""),
                "Revenue Band":              row.get("Revenue Band", ""),
                "DSIR Recognition Evidence": row.get("DSIR Recognition Evidence", ""),
                "Promoter / Decision-Maker": row.get("Promoter/Decision-Maker", ""),
                "Growth Signals":            row.get("Growth Signals", ""),
                "Source":                    "data.csv",
            })

    write_csv(FINAL_OUT, merged, FINAL_FIELDS)
    return merged


# ── phase 2: fill remaining promoters from PDFs ───────────────────────────────

def phase2_fill_promoters(merged: list[dict[str, str]]) -> None:
    links_rows = read_csv(DRHP_LINKS_CSV) if DRHP_LINKS_CSV.exists() else []
    links_by_key: dict[str, dict[str, str]] = {}
    for row in links_rows:
        links_by_key.setdefault(norm(row.get("company_name", "")), row)

    promoter_cache = load_promoter_cache()

    # find rows still needing promoters
    pending = [
        row for row in merged
        if row.get("Source", "").startswith("BSE DRHP")
        and row.get("Promoter / Decision-Maker", "") in GENERIC_VALUES
    ]
    if not pending:
        print("All promoters already filled — no PDF downloads needed.", flush=True)
        return

    print(f"Phase 2: extracting promoters from PDFs for {len(pending)} remaining companies.", flush=True)

    def work(row: dict[str, str]) -> tuple[str, str]:
        key = norm(row.get("Company Name", ""))
        if key in promoter_cache and promoter_cache[key] not in GENERIC_VALUES:
            return key, promoter_cache[key]
        link = links_by_key.get(key)
        if not link:
            return key, "No DRHP download link found."
        pdf = get_pdf_bytes(link.get("drhp_or_draft_document_url", ""), key)
        if not pdf:
            return key, "DRHP PDF could not be downloaded."
        return key, extract_promoters(extract_text(pdf))

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(work, row): row for row in pending}
        for fut in as_completed(futures):
            key, value = fut.result()
            promoter_cache[key] = value
            done += 1
            if done % 10 == 0:
                save_promoter_cache(promoter_cache)
                # refresh merged list with new promoters
                for row in merged:
                    k2 = norm(row.get("Company Name", ""))
                    if k2 in promoter_cache:
                        row["Promoter / Decision-Maker"] = promoter_cache[k2]
                write_csv(FINAL_OUT, merged, FINAL_FIELDS)
                print(f"  phase 2 progress {done}/{len(pending)}", flush=True)

    save_promoter_cache(promoter_cache)
    # final refresh
    for row in merged:
        k2 = norm(row.get("Company Name", ""))
        if k2 in promoter_cache:
            row["Promoter / Decision-Maker"] = promoter_cache[k2]
    write_csv(FINAL_OUT, merged, FINAL_FIELDS)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.time()
    print("Phase 1: building merged CSV with cached data …", flush=True)
    merged = phase1_build_merged()
    filled = sum(1 for r in merged if r.get("Promoter / Decision-Maker", "") not in GENERIC_VALUES)
    total  = len(merged)
    print(f"  {FINAL_OUT.name}: {total} rows, {filled} with promoter data.", flush=True)

    phase2_fill_promoters(merged)

    # final write
    write_csv(FINAL_OUT, merged, FINAL_FIELDS)
    elapsed = time.time() - t0
    filled2 = sum(1 for r in merged if r.get("Promoter / Decision-Maker", "") not in GENERIC_VALUES)
    print(
        f"\nDone in {elapsed:.0f}s. {FINAL_OUT.name}: {len(merged)} rows, "
        f"{filled2} with promoter data.",
        flush=True,
    )


if __name__ == "__main__":
    main()
