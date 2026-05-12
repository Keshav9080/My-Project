"""Extract company names from BSE SME DRHP HTML snapshot."""
import re
from pathlib import Path

HTML = Path(__file__).with_name("bsesme_drhp.html")
html = HTML.read_text(encoding="utf-8", errors="replace")
pat = re.compile(r'TTRow_left"><a class="tablebluelink">([^<]+)</a>')
names = [n.strip() for n in pat.findall(html)]
unique = sorted(set(names))
out_lines = [
    f"total_rows={len(names)}",
    f"unique_names={len(unique)}",
    "---names---",
    *unique,
]
Path(__file__).with_name("drhp_company_names.txt").write_text(
    "\n".join(out_lines), encoding="utf-8"
)
print(f"wrote drhp_company_names.txt rows={len(names)} unique={len(unique)}")
