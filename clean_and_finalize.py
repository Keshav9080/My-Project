"""
Clean promoter strings in final_merged_dsir_promoters.csv and write final output.
Also updates data_with_dsir_recognition.csv to ensure it has the column.
"""
from __future__ import annotations
import csv, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FINAL_IN  = ROOT / "final_merged_dsir_promoters.csv"
DATA_DSIR = ROOT / "data_with_dsir_recognition.csv"

NOISE = re.compile(
    r"\b(PROMOTERS? OF|OUR PROMOTERS?|AND THE PROMOTERS?|HEREIN|INDIVIDUAL|"
    r"ARE AS FOLLOWS|ARE SET OUT|FURTHER DETAILS|PLEASE REFER|FOR MORE|"
    r"DETAILS ARE|AS PROVIDED|MENTIONED ABOVE|MENTIONED BELOW|SEE CHAPTER|"
    r"DESCRIPTION OF|REFER TO|AS STATED)\b.*",
    re.I,
)
TRAILING_JUNK = re.compile(r"[;,.\s]+$")


def clean_promoter(value: str) -> str:
    if not value or not value.strip():
        return value

    # Fix common encoding artefacts
    value = (value
             .replace("\u2018", "'").replace("\u2019", "'")
             .replace("\u2013", "-").replace("\u2014", "-")
             .replace("\u00e2\u0080\u0099", "'")
             .replace("\ufffd", ""))

    # Strip prefix "DRHP: " for processing, add back later
    prefix = ""
    if value.startswith("DRHP: "):
        prefix = "DRHP: "
        value = value[6:]

    # Remove noise phrases
    value = NOISE.sub("", value)

    # Split on semicolons, deduplicate, clean each name
    parts: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;|]+", value):
        part = TRAILING_JUNK.sub("", part.strip())
        # Remove very short or purely numeric/junk tokens
        if len(part) < 5 or re.fullmatch(r"[0-9\s.,-]+", part):
            continue
        # Normalise whitespace
        part = re.sub(r"\s{2,}", " ", part).strip()
        key = re.sub(r"\s+", " ", part).upper()
        if key not in seen:
            seen.add(key)
            parts.append(part)

    if not parts:
        return "Verify in IPO prospectus or LinkedIn."

    return prefix + "; ".join(parts)


def main() -> None:
    for path in (FINAL_IN, DATA_DSIR):
        if not path.exists():
            print(f"Skipping {path.name} – not found.")
            continue

        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        if not rows:
            continue

        # Detect the promoter column name
        promoter_col = next(
            (c for c in rows[0] if "promoter" in c.lower() or "decision" in c.lower()),
            None,
        )
        changed = 0
        for row in rows:
            if promoter_col and row.get(promoter_col):
                cleaned = clean_promoter(row[promoter_col])
                if cleaned != row[promoter_col]:
                    row[promoter_col] = cleaned
                    changed += 1

        fieldnames = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        print(f"Wrote {path.name}: {len(rows)} rows, {changed} promoter fields cleaned.")


if __name__ == "__main__":
    main()
