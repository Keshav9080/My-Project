"""
Merge/fill CIN into icp_step9_clean.csv from:
  1) Union of pipeline CSVs that already carry CIN (same schema).
  2) Concatenated source-row blobs (drhp_companies_specified_format,
     final_merged_dsir_promoters, data_with_dsir_recognition).
  3) Regex scan across existing Step 9 text columns.
  4) Optional DRHP PDF fetch via drhp_rows_with_links.csv (latest dated URL per company).

Preserves column order and UTF-8 BOM for Excel compatibility.

Usage:
  python merge_icp_step9_cin.py              # fast: no downloads
  python merge_icp_step9_cin.py --fetch-drhp  # pulls DRHP PDFs for matching issuers (slow)
"""
from __future__ import annotations

import argparse
import csv
import io
import time
from datetime import datetime
from pathlib import Path

from enrich_drhp_columns import fetch_bytes
from merge_unified_icp import extract_cin
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent

STEP9_IN = ROOT / "icp_step9_clean.csv"
STEP9_OUT = ROOT / "icp_step9_clean.csv"
SUMMARY_OUT = ROOT / "icp_step9_cin_merge_summary.txt"

DRHP_LINKS = ROOT / "drhp_rows_with_links.csv"

CIN_SOURCES = (
    ROOT / "unified_icp_master.csv",
    ROOT / "icp_shortlist_step8_clean.csv",
    ROOT / "icp_shortlist_top1000_llm_scored.csv",
    ROOT / "icp_shortlist_top1000.csv",
    ROOT / "icp_step9_flagged.csv",
)

RAW_SOURCES = (
    ROOT / "drhp_companies_specified_format.csv",
    ROOT / "final_merged_dsir_promoters.csv",
    ROOT / "data_with_dsir_recognition.csv",
)

STEP9_TEXT_COLS = (
    "Promoter/Decision-Maker",
    "City",
    "Growth Signals",
    "Differentiation Evidence",
    "Tailwinds",
    "Revenue Band",
    "Industry Segment (raw)",
    "Evidence Tags Normalized",
    "Website",
    "Industry Segment",
)

DATE_FMT = "%d/%m/%Y"


def _parse_drhp_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), DATE_FMT)
    except ValueError:
        return None


def load_union_cin_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for path in CIN_SOURCES:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                cn = (row.get("Company Name") or "").strip()
                cin = (row.get("CIN") or "").strip()
                if cn and cin:
                    k = cn.lower()
                    if k in m and m[k] != cin:
                        raise ValueError(f"CIN conflict for {cn!r}: {m[k]} vs {cin}")
                    m[k] = cin
    return m


def load_mega_blob_map() -> dict[str, list[str]]:
    by_name: dict[str, list[str]] = {}
    for path in RAW_SOURCES:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                cn = (
                    row.get("Company Name")
                    or row.get("Company name")
                    or ""
                ).strip()
                if not cn:
                    continue
                blob = " ".join(str(v or "") for v in row.values())
                by_name.setdefault(cn.lower(), []).append(blob)
    return by_name


def latest_drhp_url_by_company() -> dict[str, str]:
    """company_name.lower() -> URL with greatest filing date."""
    if not DRHP_LINKS.exists():
        return {}
    best: dict[str, tuple[datetime | None, str]] = {}
    with DRHP_LINKS.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            cn = (row.get("company_name") or "").strip()
            url = (row.get("drhp_or_draft_document_url") or "").strip()
            ds = (row.get("drhp_or_draft_date") or "").strip()
            if not cn or not url:
                continue
            dt = _parse_drhp_date(ds)
            k = cn.lower()
            cur = best.get(k)
            if cur is None or (dt and cur[0] and dt > cur[0]) or (
                dt and cur[0] is None
            ):
                best[k] = (dt, url)
            elif cur[0] is None and dt is None:
                best[k] = (None, url)
    return {k: v[1] for k, v in best.items()}


def pdf_text_first_pages(
    data: bytes,
    max_pages: int = 14,
    wall_sec: float = 22.0,
) -> str:
    """Bounded extraction — avoids stalls on huge / malformed SME PDFs."""
    t0 = time.monotonic()
    reader = PdfReader(io.BytesIO(data), strict=False)
    texts: list[str] = []
    n = min(max_pages, len(reader.pages))
    for i in range(n):
        if time.monotonic() - t0 > wall_sec:
            break
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception:
            t = ""
        texts.append(t)
    return "\n".join(texts)


def cin_from_drhp_pdf(url: str) -> str | None:
    raw = fetch_bytes(url)
    if not raw:
        return None
    if url.lower().endswith(".pdf"):
        try:
            blob = pdf_text_first_pages(raw)
        except Exception:
            return None
        return extract_cin(blob) or None
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fill CIN column in icp_step9_clean.csv.")
    p.add_argument(
        "--fetch-drhp",
        action="store_true",
        help="Download DRHP PDFs from drhp_rows_with_links.csv when CIN still blank.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    union = load_union_cin_map()
    mega_by = load_mega_blob_map()
    drhp_urls = latest_drhp_url_by_company()

    with STEP9_IN.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if not fieldnames or "CIN" not in fieldnames:
            raise SystemExit("icp_step9_clean.csv missing header or CIN column.")
        rows = list(reader)

    stats = {
        "total": len(rows),
        "already_had": 0,
        "from_union_csv": 0,
        "from_mega_blob": 0,
        "from_step9_text": 0,
        "from_full_row_scan": 0,
        "from_drhp_pdf": 0,
        "still_blank": 0,
    }
    pdf_attempts: list[tuple[str, str, str | None]] = []

    for row in rows:
        cur = (row.get("CIN") or "").strip()
        if cur:
            stats["already_had"] += 1
            continue
        cn = (row.get("Company Name") or "").strip()
        k = cn.lower()
        cin = ""

        if k in union:
            cin = union[k]
            stats["from_union_csv"] += 1
        elif k in mega_by:
            mega = " ".join(mega_by[k])
            cin = extract_cin(mega) or ""
            if cin:
                stats["from_mega_blob"] += 1

        if not cin:
            blob = " ".join((row.get(c) or "") for c in STEP9_TEXT_COLS)
            cin = extract_cin(blob) or ""
            if cin:
                stats["from_step9_text"] += 1

        if not cin:
            blob_all = " ".join(
                str(v or "") for kk, v in row.items() if kk != "CIN"
            )
            cin = extract_cin(blob_all) or ""
            if cin:
                stats["from_full_row_scan"] += 1

        if (
            args.fetch_drhp
            and not cin
            and k in drhp_urls
        ):
            url = drhp_urls[k]
            got = cin_from_drhp_pdf(url)
            pdf_attempts.append((cn, url, got))
            if got:
                cin = got
                stats["from_drhp_pdf"] += 1

        row["CIN"] = cin

        if not (row.get("CIN") or "").strip():
            stats["still_blank"] += 1

    with STEP9_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    lines = [
        "merge_icp_step9_cin.py summary",
        f"total_rows={stats['total']}",
        f"already_had_cin={stats['already_had']}",
        f"filled_from_union_pipeline_csv={stats['from_union_csv']}",
        f"filled_from_source_mega_blob={stats['from_mega_blob']}",
        f"filled_from_step9_text_columns={stats['from_step9_text']}",
        f"filled_from_full_row_regex_scan={stats['from_full_row_scan']}",
        f"filled_from_drhp_pdf_fetch={stats['from_drhp_pdf']}",
        f"still_blank={stats['still_blank']}",
        "",
        f"fetch_drhp_pdf={bool(args.fetch_drhp)}",
        "DRHP PDF attempts:",
    ]
    for cn, url, got in pdf_attempts:
        lines.append(f"  {cn}: got={got or 'NONE'}")
        lines.append(f"    url={url}")

    SUMMARY_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
