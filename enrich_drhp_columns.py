"""
Fill enrichment columns by downloading BSE SME DRHP PDFs (and PDFs inside ZIPs)
and applying heuristic text extraction. Scanned/image-only PDFs yield blanks.

Usage:
  pip install -r requirements-enrichment.txt
  python enrich_drhp_columns.py

Outputs drhp_companies_enriched.csv (checkpoint drhp_enrichment_checkpoint.json).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "drhp_rows_with_links.csv"
OUTPUT_CSV = ROOT / "drhp_companies_specified_format.csv"
CHECKPOINT = ROOT / "drhp_enrichment_checkpoint.json"
CACHE_DIR = ROOT / "_drhp_download_cache"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
CURL_BIN = shutil.which("curl") or shutil.which("curl.exe")
MAX_WORKERS = 8
REQUEST_TIMEOUT = 60
MAX_PDF_PAGES = 22  # early sections only — speeds run & avoids parser hangs on huge files
EXTRACT_TIMEOUT_SEC = 30  # thread-bound; pypdf can stall on some SME scans
FETCH_RETRIES = 1

COLS = [
    "Company name",
    "City / Location",
    "Industry Segment",
    "Website",
    "Revenue Band",
    "DSIR Recognition Evidence",
    "Promoter / Decision-Maker",
    "Growth Signals",
]


def blank_output(company: str) -> dict[str, str]:
    row = {k: "" for k in COLS}
    row["Company name"] = company.strip()
    return row


def fetch_bytes(url: str) -> bytes | None:
    """Prefer curl (reliable on BSE SME host); fallback to requests."""
    last_err: str | None = None
    for attempt in range(FETCH_RETRIES):
        if CURL_BIN:
            fd, tmp_path = tempfile.mkstemp(suffix=".dl")
            os.close(fd)
            try:
                subprocess.run(
                    [
                        CURL_BIN,
                        "-sL",
                        "--fail",
                        "-m",
                        str(REQUEST_TIMEOUT),
                        "-A",
                        HEADERS["User-Agent"],
                        "-o",
                        tmp_path,
                        url,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=REQUEST_TIMEOUT + 15,
                )
                data = Path(tmp_path).read_bytes()
                if data and len(data) > 400:
                    return data
                last_err = "short payload"
            except subprocess.CalledProcessError as e:
                last_err = (e.stderr or b"").decode("utf-8", "replace")[:120]
            except subprocess.TimeoutExpired:
                last_err = "curl subprocess timeout"
            except OSError as e:
                last_err = str(e)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        try:
            r = requests.get(
                url,
                headers=HEADERS,
                timeout=(20, REQUEST_TIMEOUT),
            )
            if r.status_code == 200 and len(r.content) > 400:
                return r.content
            last_err = f"http {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)[:120]
        time.sleep(1.5 + attempt)
    return None


def pdf_bytes_from_archive(data: bytes) -> bytes | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
        if not pdfs:
            return None
        pdfs.sort(key=lambda x: ("drhp" not in x.lower(), -len(x)))
        return z.read(pdfs[0])
    except zipfile.BadZipFile:
        return None


def extract_pdf_text(data: bytes) -> str:
    """Bounded extraction; returns '' on timeout or hard PDF errors."""
    parts: list[str] = []
    err: list[str] = []

    def _run() -> None:
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            n = min(len(reader.pages), MAX_PDF_PAGES)
            for i in range(n):
                try:
                    t = reader.pages[i].extract_text()
                    if t:
                        parts.append(t)
                except Exception:
                    continue
        except Exception as e:
            err.append(str(e))

    import threading

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=EXTRACT_TIMEOUT_SEC)
    if th.is_alive():
        return ""
    if err:
        return ""
    return "\n".join(parts)


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clip(s: str, max_len: int = 500) -> str:
    s = normalize_ws(s)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def extract_registered_snippet(text: str) -> str:
    def trim_addr(raw: str, lim: int = 420) -> str:
        s = clip(raw, lim)
        for sp in (
            r"\s+for\s+\d+\s+months",
            r"\s+The\s+lease\s+deed",
            r"\s+Our\s+Company\s+has\s+taken",
        ):
            parts = re.split(sp, s, maxsplit=1, flags=re.I)
            s = parts[0].strip()
        return s

    patterns = [
        r"(?:Registered\s+Office|Registrar\s+of\s+Companies)[^\n]{0,80}\n(.{20,400}?)(?=\n\s*\n|Our\s+History|CIN|Corporate\s+Information|$)",
        r"Registered\s+office\s+(?:of\s+our\s+Company\s+)?(?:is\s+)?(?:situated\s+)?(?:at\s+)?(.{15,350}?)(?=\n\n|\.(?:\s|$)|Website)",
        r"(?:Address\s+of\s+the\s+)?Registered\s+Office[:\s]+(.{15,400}?)(?=\n\n|CIN|Tel\.|Email)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.DOTALL)
        if m:
            return trim_addr(m.group(1))
    m = re.search(
        r"([A-Za-z0-9\s,\.\-\(\)/&]{25,120},\s*(?:India\s*)?\d{6})",
        text[:12000],
    )
    if m:
        return trim_addr(m.group(1))
    return ""


def extract_industry(text: str) -> str:
    for pat in (
        r"(?:Industry\s+segment|Business\s+of\s+our\s+Company|Overview\s+of\s+our\s+Business)[^\n]*\n(.{40,600}?)(?=\n\s*\n|Our\s+)",
        r"(?:We\s+are\s+(?:an\s+)?(?:engaged\s+in|a\s+))(.{30,400}?)(?=\n|\.(?:\s+[A-Z]|$))",
    ):
        m = re.search(pat, text[:25000], re.I | re.DOTALL)
        if m:
            return clip(m.group(1), 450)
    return ""


def extract_websites(text: str) -> str:
    urls = set(
        re.findall(
            r"(?:https?://|www\.)[a-zA-Z0-9][-a-zA-Z0-9./_%?=&+#]*",
            text[:15000],
        )
    )
    bad = {"www.sebi.gov.in", "www.bseindia.com", "www.nseindia.com"}
    urls = {u.rstrip(".,);") for u in urls if not any(b in u.lower() for b in bad)}
    urls = {u for u in urls if len(u) > 6}
    if not urls:
        return ""
    return "; ".join(sorted(urls)[:5])


def extract_revenue_band(text: str) -> str:
    chunk = text[:80000]
    candidates: list[str] = []
    for pat in (
        r"(?:Total\s+(?:revenue|income|operating\s+income)\s*(?:from\s+operations)?)[^\d]{0,40}([\d,\.]+\s*(?:crores?|lakhs?|Cr\.?|Lacs?))",
        r"(?:Revenue\s+from\s+operations)[^\d]{0,40}([\d,\.]+\s*(?:crores?|lakhs?))",
        r"(?:Restated\s+)?(?:statement\s+of\s+)?(?:profit\s+and\s+loss)[\s\S]{0,2000}?([\d,\.]+\s*(?:crores?|lakhs?))",
    ):
        for m in re.finditer(pat, chunk, re.I):
            candidates.append(normalize_ws(m.group(1)))
    if not candidates:
        return ""
    # prefer crore-scale figures that look like totals (rough heuristic)
    crores = [c for c in candidates if "crore" in c.lower() or "cr" in c.lower()]
    pool = crores or candidates
    return clip(pool[0], 120)


def extract_dsir(text: str) -> str:
    low = text.lower()
    if "dsir" not in low and "department of scientific and industrial research" not in low:
        return "No DSIR / R&D recognition wording detected in sampled DRHP pages."
    m = re.search(
        r"((?:DSIR|Department\s+of\s+Scientific\s+and\s+Industrial\s+Research)[^.]{10,400}\.)",
        text,
        re.I | re.DOTALL,
    )
    if m:
        return clip(m.group(1), 500)
    return "DRHP references DSIR / industrial research — verify exact status in full document."


def extract_promoters(text: str) -> str:
    window = re.search(
        r"(?:details\s+of\s+)?(?:our\s+)?promoters?[:\s]*([\s\S]{40,3500}?)(?=promoter\s+group|details\s+of\s+our\s+promoter\s+group|our\s+management|capital\s+structure)",
        text[:60000],
        re.I,
    )
    blob = window.group(1) if window else text[:60000]
    names = set()
    for m in re.finditer(
        r"(?:^|\n)\s*(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})",
        blob,
    ):
        names.add(m.group(0).strip())
    for m in re.finditer(
        r"(?:Name\s+of\s+(?:the\s+)?Promoter|Promoter\s+Name)[^\w]{0,15}([A-Z][^\n]{4,80}?)(?=\n|$)",
        blob,
        re.I,
    ):
        names.add(normalize_ws(m.group(1)))
    if not names:
        return ""
    out = "; ".join(sorted(names)[:12])
    return clip(out, 500)


def extract_growth_signals(text: str) -> str:
    chunk = text[:70000].lower()
    hits: list[str] = []
    kw_map = [
        ("capacity expansion", "capacity expansion"),
        ("capex", "capex / capital expenditure"),
        ("iso ", "ISO certification mentioned"),
        ("recruitment", "hiring / recruitment"),
        ("linkedin", "digital / hiring channel mention"),
        ("subsidiary", "subsidiary / group structure"),
        ("new facility", "facility expansion"),
        ("backward integration", "backward integration"),
        ("forward integration", "forward integration"),
    ]
    for needle, label in kw_map:
        if needle in chunk:
            hits.append(label)
    if not hits:
        return "No explicit expansion/hiring/certification keywords in sampled pages — check MD&A / risk factors in full DRHP."
    return "; ".join(dict.fromkeys(hits))


def process_row(row: dict[str, str]) -> dict[str, str]:
    url = row["drhp_or_draft_document_url"].strip()
    company = row["company_name"].strip()
    base = {
        "Company name": company,
        "City / Location": "",
        "Industry Segment": "",
        "Website": "",
        "Revenue Band": "",
        "DSIR Recognition Evidence": "",
        "Promoter / Decision-Maker": "",
        "Growth Signals": "",
    }
    if not url:
        base["City / Location"] = "No document URL."
        return base

    raw = fetch_bytes(url)
    if not raw:
        base["City / Location"] = "Download failed (timeout or HTTP error)."
        return base

    if url.lower().endswith(".zip"):
        pdf_data = pdf_bytes_from_archive(raw)
        if not pdf_data:
            base["City / Location"] = "ZIP contained no PDF."
            return base
    else:
        pdf_data = raw

    try:
        text = extract_pdf_text(pdf_data)
    except Exception as e:
        base["City / Location"] = f"PDF parse error: {type(e).__name__}"
        return base

    if len(text.strip()) < 80:
        base["City / Location"] = (
            "Little or no extractable text (likely scanned PDF — needs OCR)."
        )
        base["DSIR Recognition Evidence"] = "Not assessed (no machine-readable text)."
        base["Growth Signals"] = "Not assessed (no machine-readable text)."
        return base

    base["City / Location"] = extract_registered_snippet(text) or "See DRHP ‘General Information’ — address not matched by pattern."
    base["Industry Segment"] = extract_industry(text) or "See DRHP ‘Industry / Business overview’ section."
    base["Website"] = extract_websites(text) or "Search DRHP / issuer for corporate website."
    base["Revenue Band"] = extract_revenue_band(text) or "Extract from restated financials in DRHP (pattern not matched)."
    base["DSIR Recognition Evidence"] = extract_dsir(text)
    base["Promoter / Decision-Maker"] = extract_promoters(text) or "See DRHP ‘Promoters’ and ‘Management’ sections."
    base["Growth Signals"] = extract_growth_signals(text)
    return base


def load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {"done": {}, "last_update": None}


def save_checkpoint(cp: dict[str, Any]) -> None:
    CHECKPOINT.write_text(json.dumps(cp, indent=0), encoding="utf-8")


def flush_output(rows_in: list[dict[str, str]], done: dict[str, dict[str, str]]) -> None:
    merged = [
        done[str(r["row_index"])]
        if str(r["row_index"]) in done
        else blank_output(r["company_name"])
        for r in rows_in
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for row in merged:
            w.writerow({k: row.get(k, "") for k in COLS})


def main() -> None:
    rows_in = []
    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        rows_in = list(csv.DictReader(f))

    limit_raw = os.environ.get("ENRICH_LIMIT", "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else None

    cp = load_checkpoint()
    done: dict[str, dict] = cp.get("done", {})

    pending = [r for r in rows_in if str(r.get("row_index", "")) not in done]
    if limit is not None:
        pending = pending[:limit]

    print(
        f"Total rows: {len(rows_in)} | Resume done: {len(done)} | "
        f"This run pending: {len(pending)}",
        flush=True,
    )

    def worker(r: dict[str, str]) -> tuple[str, dict[str, str]]:
        idx = str(r["row_index"])
        out = process_row(r)
        time.sleep(0.12)
        return idx, out

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker, r): r for r in pending}
        for i, fut in enumerate(as_completed(futures), 1):
            idx, result = fut.result()
            done[idx] = result
            cp["done"] = done
            cp["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if i % 3 == 0:
                save_checkpoint(cp)
                flush_output(rows_in, done)
                print(f"checkpoint … {i}/{len(pending)}", flush=True)
            sys.stdout.flush()

    save_checkpoint(cp)

    flush_output(rows_in, done)

    merged_len = len(rows_in)
    print(f"Wrote {OUTPUT_CSV} ({merged_len} rows)", flush=True)


if __name__ == "__main__":
    main()
