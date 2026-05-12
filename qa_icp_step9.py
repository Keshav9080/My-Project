"""
Step 9: Automated QA flags on icp_shortlist_step8_clean.csv

Outputs:
  - icp_step9_flagged.csv   (at least one QA tag)
  - icp_step9_clean.csv     (no QA tags)
  - icp_step9_summary.txt

Reference date for "stale" = 3-year lookback from QA_REFERENCE_DATE (default: 2026-05-12).
"""
from __future__ import annotations

import argparse
import csv
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_IN = ROOT / "icp_shortlist_step8_clean.csv"
OUT_FLAGGED = ROOT / "icp_step9_flagged.csv"
OUT_CLEAN = ROOT / "icp_step9_clean.csv"
OUT_SUMMARY = ROOT / "icp_step9_summary.txt"

CIN_YEAR_RE = re.compile(
    r"\b[LUlu]\d{5}[A-Za-z]{2}(\d{4})[A-Za-z]{3}\d{6}\b",
)
YEAR_TOKEN_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
EXPIRED_RE = re.compile(
    r"expired|laps(ed|e)|not\s+renew(ed|al)|withdrawn\s+cert|revoked\s+cert|"
    r"certificate\s+(withdrawn|cancelled)|surveillance\s+audit\s+fail",
    re.I,
)
HYPE_RE = re.compile(
    r"\b(largest|leading\s+player|world-?\s*class|no\.?\s*1|number\s+one|only\s+company\s+to|"
    r"unparalleled|best-?in-?class)\b",
    re.I,
)
GROWTH_EVIDENCE_RE = re.compile(
    r"ipo\s+filed|capex|capacity\s+expansion|new\s+facility|greenfield|plant|hiring|recruitment|"
    r"funded\s+expansion|order\s+book|backward\s+integration|forward\s+integration|expansion\b",
    re.I,
)
SECTOR_MOAT_RE = re.compile(
    r"dsir|patent|rnd_mention|proprietary|oem\s+for|defence\s+clearance|fda|ce\s+mark|"
    r"drdo|isro|niche\s+process",
    re.I,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 9 QA flagging for ICP shortlist.")
    p.add_argument("--input", type=Path, default=DEFAULT_IN)
    p.add_argument("--out-flagged", type=Path, default=OUT_FLAGGED)
    p.add_argument("--out-clean", type=Path, default=OUT_CLEAN)
    p.add_argument("--out-summary", type=Path, default=OUT_SUMMARY)
    p.add_argument(
        "--ref-date",
        type=str,
        default="2026-05-12",
        help="ISO date; narrative years earlier than (ref_date - 3 years) trigger stale flags.",
    )
    p.add_argument(
        "--target-flag-min",
        type=int,
        default=300,
        help="If fewer rows flagged after base rules, add calibration rule until this minimum.",
    )
    p.add_argument(
        "--target-flag-max",
        type=int,
        default=400,
        help="If more than this many flagged, drop lowest-priority calibration flags first.",
    )
    return p.parse_args()


def strip_cin_years(text: str, cin: str) -> str:
    """Remove CIN tokens and their embedded years to avoid false stale hits."""
    t = text
    if cin:
        t = re.sub(re.escape(cin), " ", t, flags=re.I)
    for m in CIN_YEAR_RE.finditer(t):
        t = t.replace(m.group(0), " ")
    return t


def narrative_years(blob: str) -> set[int]:
    return {int(y) for y in YEAR_TOKEN_RE.findall(blob)}


def qa_rules(
    row: dict[str, str],
    ref: date,
) -> list[str]:
    tags: list[str] = []

    stale_year_max = ref.year - 4  # e.g. 2022 when ref is 2026-05

    diff_ev = (row.get("Differentiation Evidence") or "").lower()
    ev_norm = (row.get("Evidence Tags Normalized") or "").lower()
    growth = (row.get("Growth Signals") or "").lower()
    city = row.get("City") or ""
    raw_ind = (row.get("Industry Segment (raw)") or "").lower()
    ind_seg = (row.get("Industry Segment") or "").lower()
    tail = (row.get("Tailwinds") or "").lower()
    web = row.get("Website") or ""
    cin = (row.get("CIN") or "").strip()

    blob_for_years = strip_cin_years(
        f"{city}\n{growth}\n{diff_ev}\n{row.get('Differentiation Evidence') or ''}",
        cin,
    )
    years = narrative_years(blob_for_years)
    if any(y <= stale_year_max for y in years):
        tags.append("stale_temporal_signal")
    elif any(y == ref.year - 3 for y in years):
        tags.append("borderline_stale_temporal_signal")

    if EXPIRED_RE.search(f"{diff_ev} {growth} {city}"):
        tags.append("certification_or_license_expiry_language")

    try:
        c3 = int(row.get("C3_Differentiation") or 0)
    except ValueError:
        c3 = 0
    try:
        c6 = int(row.get("C6_Growth_Signals") or 0)
    except ValueError:
        c6 = 0

    has_iso_struct = (
        "iso_certification" in ev_norm
        or "iso" in diff_ev
        or "iso certification" in growth
    )
    has_sector_moat = bool(SECTOR_MOAT_RE.search(ev_norm + " " + diff_ev))

    if c3 == 1 and has_iso_struct and not has_sector_moat:
        tags.append("iso_only_differentiation_no_sector_moat")

    if has_iso_struct and "dsir" not in ev_norm and "patent" not in ev_norm and "rnd" not in ev_norm:
        if c3 == 1 and "iso_only_differentiation_no_sector_moat" not in tags:
            tags.append("borderline_inflated_cert_story")

    if c6 == 1 and not GROWTH_EVIDENCE_RE.search(growth) and "ipo filed" not in tail:
        if HYPE_RE.search(growth + " " + tail):
            tags.append("growth_superlatives_weak_evidence")
        elif re.search(r"subsidiary|group structure", growth) and "ipo" not in tail:
            tags.append("growth_signal_mostly_corporate_shell")

    if HYPE_RE.search(growth + " " + diff_ev + " " + tail) and not GROWTH_EVIDENCE_RE.search(growth):
        tags.append("exaggerated_language_low_substance")

    drhp_generic = (
        "unspecified_see_drhp" in ind_seg or "see drhp" in raw_ind or "see drhp" in diff_ev
    )
    if drhp_generic and "no_structured" in diff_ev and not has_sector_moat:
        tags.append("weak_differentiation_drhp_generics")

    url_parts = [p.strip() for p in re.split(r"[;,\|]+", web) if p.strip()]
    corp_like_urls = [p for p in url_parts if not re.search(r"bigshare|kfintech|maashitla|skyline|narnolia", p, re.I)]
    if len(corp_like_urls) <= 1 and drhp_generic and len(growth) < 80:
        tags.append("generic_web_or_thin_ops_narrative")

    # Dedup unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def main() -> None:
    args = parse_args()
    inp = args.input.resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    y, m, d = (int(x) for x in args.ref_date.split("-"))
    ref = date(y, m, d)

    with inp.open(newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        fields = rdr.fieldnames or []
        rows = list(rdr)

    extra = ["Step9_QA_Tags", "Step9_QA_Flag_Count"]
    out_fields = [c for c in fields if c] + [c for c in extra if c not in fields]

    tagged: list[tuple[dict[str, str], list[str]]] = []
    for row in rows:
        base_tags = qa_rules(row, ref)
        tagged.append((row, base_tags))

    n_base_flagged = sum(1 for _, t in tagged if t)
    clean_idx = [i for i, (_, t) in enumerate(tagged) if not t]
    need = max(0, args.target_flag_min - n_base_flagged)
    room = max(0, args.target_flag_max - n_base_flagged)
    cal_n = min(need, room, len(clean_idx))

    CAL_TAG = "calibration_review_queue_step10"

    def row_score(i: int) -> float:
        try:
            return float(tagged[i][0].get("ICP_Blended_Weighted_Score") or 0)
        except ValueError:
            return 0.0

    for i in sorted(clean_idx, key=row_score)[:cal_n]:
        row, ts = tagged[i]
        tagged[i] = (row, list(ts) + [CAL_TAG])

    n_flagged = sum(1 for _, t in tagged if t)
    if n_base_flagged > args.target_flag_max:
        print(
            f"Warning: base QA rules alone flagged {n_base_flagged} rows (over --target-flag-max); "
            "consider relaxing rules."
        )

    clean_rows: list[dict[str, str]] = []
    flag_rows: list[dict[str, str]] = []
    for row, ts in tagged:
        out = dict(row)
        out["Step9_QA_Tags"] = "|".join(ts)
        out["Step9_QA_Flag_Count"] = str(len(ts))
        if ts:
            flag_rows.append({k: out.get(k, "") for k in out_fields})
        else:
            clean_rows.append({k: out.get(k, "") for k in fields if k})

    def sort_name(r: dict[str, str]) -> str:
        return (r.get("Company Name") or "").upper()

    clean_rows.sort(key=sort_name)
    flag_rows.sort(key=sort_name)

    out_flag = args.out_flagged.resolve()
    out_clean = args.out_clean.resolve()
    out_sum = args.out_summary.resolve()

    with out_flag.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(flag_rows)

    with out_clean.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(clean_rows)

    n_in = len(rows)
    n_clean = len(clean_rows)

    tag_counts: dict[str, int] = {}
    for _, ts in tagged:
        for t in ts:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    lines = [
        "Step 9 QA",
        f"input_file={inp.name}",
        f"QA_reference_date={args.ref_date}",
        f"narrative_years_flag_stale_if_year_le={ref.year - 4}",
        f"total_input={n_in}",
        f"total_flagged_base_rules={n_base_flagged}",
        f"total_calibration_added={cal_n}",
        f"total_flagged={n_flagged}",
        f"total_retained_clean={n_clean}",
        "",
        "QA tag counts:",
    ]
    for t, c in sorted(tag_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {t}={c}")
    lines.append("")
    out_sum.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        f"Step 9: input={n_in}, flagged_base={n_base_flagged}, "
        f"calibration_added={cal_n}, flagged_total={n_flagged}, clean={n_clean}"
    )
    print(f"Wrote {out_flag.name}, {out_clean.name}, {out_sum.name}")


if __name__ == "__main__":
    main()
