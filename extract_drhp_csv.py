"""Pair each DRHP grid row with document URL and filing date."""
import csv
import re
from pathlib import Path

HTML = Path(__file__).with_name("bsesme_drhp.html")
html = HTML.read_text(encoding="utf-8", errors="replace")
rows = html.split('<tr class="TTRow">')[1:]
out_path = Path(__file__).with_name("drhp_rows_with_links.csv")

records = []
for i, chunk in enumerate(rows):
    cm = re.search(
        r'TTRow_left"><a class="tablebluelink">([^<]+)</a>', chunk
    )
    if not cm:
        continue
    company = cm.group(1).strip()
    href_m = re.search(r'ContentPlaceHolder1_gvData_hyDRHP_\d+" href="([^"]+)"', chunk)
    date_m = re.search(
        r'ContentPlaceHolder1_gvData_hyDRHP_\d+"[^>]*>([^<]+)</a>', chunk
    )
    drhp_url = href_m.group(1).strip() if href_m else ""
    drhp_date = date_m.group(1).strip() if date_m else ""
    records.append(
        {
            "row_index": len(records),
            "company_name": company,
            "drhp_or_draft_date": drhp_date,
            "drhp_or_draft_document_url": drhp_url,
        }
    )

with out_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(
        f,
        fieldnames=[
            "row_index",
            "company_name",
            "drhp_or_draft_date",
            "drhp_or_draft_document_url",
        ],
    )
    w.writeheader()
    w.writerows(records)

print(f"wrote {out_path.name} records={len(records)}")
