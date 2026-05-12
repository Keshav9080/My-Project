import csv
from pathlib import Path

rows = list(csv.DictReader(open("final_merged_dsir_promoters.csv", encoding="utf-8-sig")))
print("=== FINAL_MERGED_DSIR_PROMOTERS.CSV ===")
print(f"Total rows : {len(rows)}")
print(f"Columns    : {list(rows[0].keys())}")
print()

SKIP_PHRASES = (
    "Verify in IPO", "not machine-extracted", "No DRHP", "Could not download",
    "No BSE DRHP", "PDF had no extractable", "not extracted",
)

for col in rows[0].keys():
    filled = sum(
        1 for r in rows
        if r.get(col, "").strip()
        and not any(ph.lower() in r.get(col, "").lower() for ph in SKIP_PHRASES)
    )
    print(f"  {col:<35} {filled}/{len(rows)} meaningful")

print()
print("=== SAMPLE ROWS (3 with DRHP promoters extracted) ===")
samples = [r for r in rows if r.get("Promoter / Decision-Maker", "").startswith("DRHP:")][:3]
for s in samples:
    company   = s.get("Company Name", "")
    city      = s.get("City / Location", "")[:80]
    segment   = s.get("Industry Segment", "")[:80]
    website   = s.get("Website", "")[:80]
    revenue   = s.get("Revenue Band", "")[:60]
    dsir      = s.get("DSIR Recognition Evidence", "")[:80]
    promoter  = s.get("Promoter / Decision-Maker", "")[:120]
    growth    = s.get("Growth Signals", "")[:80]
    print(f"Company  : {company}")
    print(f"  City     : {city}")
    print(f"  Segment  : {segment}")
    print(f"  Website  : {website}")
    print(f"  Revenue  : {revenue}")
    print(f"  DSIR     : {dsir}")
    print(f"  Promoter : {promoter}")
    print(f"  Growth   : {growth}")
    print()

# DSIR matches
dsir_yes = sum(1 for r in rows if "Matched in DSIR" in r.get("DSIR Recognition Evidence", ""))
print(f"DSIR recognised companies : {dsir_yes}")
print(f"DSIR no match             : {len(rows) - dsir_yes}")
