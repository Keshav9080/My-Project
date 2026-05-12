"""
Emit final ICP deliverables:
  - icp_final_master.csv      — full scored dataset (846) + Step8 / Step9 fields
  - icp_final_shortlist.csv  — outreach-oriented columns, Step8-retained only, cap 1000, deduped
  - icp_final_rejects.csv    — Step8 removes with normalized reason codes
  - icp_final_summary.txt    — input vs retained, sector / revenue / tailwinds, C1–C6 coverage %
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from merge_unified_icp import normalize_company_key

ROOT = Path(__file__).resolve().parent

F_SCORED = ROOT / "icp_shortlist_top1000_llm_scored.csv"
F_STEP8_CLEAN = ROOT / "icp_shortlist_step8_clean.csv"
F_STEP8_REJ = ROOT / "icp_step8_rejects.csv"
F_STEP9_CLEAN = ROOT / "icp_step9_clean.csv"
F_STEP9_FLAG = ROOT / "icp_step9_flagged.csv"

F_DRHP = ROOT / "drhp_companies_specified_format.csv"
F_MERGED = ROOT / "final_merged_dsir_promoters.csv"
F_DATA_DSIR = ROOT / "data_with_dsir_recognition.csv"

OUT_MASTER = ROOT / "icp_final_master.csv"
OUT_SHORT = ROOT / "icp_final_shortlist.csv"
OUT_REJECT = ROOT / "icp_final_rejects.csv"
OUT_SUMMARY = ROOT / "icp_final_summary.txt"

SHORTLIST_CAP = 1000

SHORTLIST_COLS = [
    "Company Name",
    "City",
    "Industry Segment",
    "Website",
    "Revenue Band",
    "CIN",
    "Promoter/Decision-Maker",
    "Tailwinds",
    "Differentiation Evidence",
    "Source",
    "Evidence Tags Normalized",
    "ICP_Criteria_Met_Count",
    "ICP_Coverage_Score",
    "ICP_Weighted_Score",
    "ICP_Blended_Weighted_Score",
    "Blended_C1",
    "Blended_C2",
    "Blended_C3",
    "Blended_C4",
    "Blended_C5",
    "Blended_C6",
    "Step9_QA_Tags",
    "Step9_QA_Flag_Count",
    "Step9_Outreach_Note",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _float_or_zero(x: str) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0


def build_master(
    scored: list[dict[str, str]],
    retained_keys: set[str],
    step9_flag: dict[str, dict[str, str]],
    step9_clean_keys: set[str],
) -> tuple[list[dict[str, str]], list[str]]:
    extra = ["Step8_Result", "Step9_QA_Tags", "Step9_QA_Flag_Count", "Step9_Status"]
    out: list[dict[str, str]] = []
    for row in scored:
        cn = (row.get("Company Name") or "").strip()
        key = cn.lower()
        nk = normalize_company_key(cn)

        row = dict(row)
        if nk and nk in retained_keys:
            row["Step8_Result"] = "retained"
        else:
            row["Step8_Result"] = "rejected"

        if key in step9_flag:
            r9 = step9_flag[key]
            row["Step9_QA_Tags"] = r9.get("Step9_QA_Tags", "")
            row["Step9_QA_Flag_Count"] = r9.get("Step9_QA_Flag_Count", "")
            row["Step9_Status"] = "flagged"
        elif key in step9_clean_keys:
            row["Step9_QA_Tags"] = ""
            row["Step9_QA_Flag_Count"] = "0"
            row["Step9_Status"] = "clean"
        else:
            row["Step9_QA_Tags"] = ""
            row["Step9_QA_Flag_Count"] = ""
            row["Step9_Status"] = "not_evaluated_step9"

        out.append(row)

    base = list(scored[0].keys()) if scored else []
    fieldnames = base + [c for c in extra if c not in base]
    return out, fieldnames


def dedupe_shortlist_rows(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep best-scoring row per normalize_company_key."""
    best_by_norm: dict[str, dict[str, str]] = {}
    for r in rows:
        nk = normalize_company_key((r.get("Company Name") or "").strip())
        if not nk:
            nk = (r.get("Company Name") or "").strip().lower()
        score = _float_or_zero(r.get("ICP_Blended_Weighted_Score", "0"))
        score_w = _float_or_zero(r.get("ICP_Weighted_Score", "0"))
        met = _float_or_zero(r.get("ICP_Criteria_Met_Count", "0"))
        cur = best_by_norm.get(nk)
        if cur is None:
            best_by_norm[nk] = r
            continue
        cs = _float_or_zero(cur.get("ICP_Blended_Weighted_Score", "0"))
        csw = _float_or_zero(cur.get("ICP_Weighted_Score", "0"))
        cmet = _float_or_zero(cur.get("ICP_Criteria_Met_Count", "0"))
        if (score, score_w, met) > (cs, csw, cmet):
            best_by_norm[nk] = r
    deduped = list(best_by_norm.values())
    deduped.sort(
        key=lambda x: (
            -_float_or_zero(x.get("ICP_Blended_Weighted_Score", "0")),
            -_float_or_zero(x.get("ICP_Weighted_Score", "0")),
            -_float_or_zero(x.get("ICP_Criteria_Met_Count", "0")),
            (x.get("Company Name") or "").lower(),
        ),
    )
    return deduped[:SHORTLIST_CAP]


def tailwind_token_counts(rows: list[dict[str, str]], limit: int = 20) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for r in rows:
        raw = (r.get("Tailwinds") or "").lower()
        for t in raw.split("|"):
            t = t.strip()
            if len(t) >= 3:
                c[t] += 1
    return c.most_common(limit)


def criterion_coverage_pct(rows: list[dict[str, str]]) -> dict[str, float]:
    if not rows:
        return {}
    keys = [
        ("C1_Manufacturer", "C1"),
        ("C2_India_Based", "C2"),
        ("C3_Differentiation", "C3"),
        ("C4_Technical_DM", "C4"),
        ("C5_Tailwinds", "C5"),
        ("C6_Growth_Signals", "C6"),
    ]
    n = len(rows)
    out: dict[str, float] = {}
    for col, label in keys:
        passed = sum(
            1
            for r in rows
            if str(r.get(col, "")).strip() in ("1", "1.0")
        )
        out[label] = round(100.0 * passed / n, 1)
    return out


def main() -> None:
    if not F_SCORED.exists():
        raise SystemExit(f"Missing {F_SCORED}")

    scored = _read_csv(F_SCORED)
    step8_clean = _read_csv(F_STEP8_CLEAN) if F_STEP8_CLEAN.exists() else []
    step8_rej = _read_csv(F_STEP8_REJ) if F_STEP8_REJ.exists() else []
    step9_clean_rows = _read_csv(F_STEP9_CLEAN) if F_STEP9_CLEAN.exists() else []
    step9_flag_rows = _read_csv(F_STEP9_FLAG) if F_STEP9_FLAG.exists() else []

    retained_keys = {
        normalize_company_key((r.get("Company Name") or "").strip())
        for r in step8_clean
    }
    retained_keys.discard("")

    step9_clean_keys = {
        (r.get("Company Name") or "").strip().lower() for r in step9_clean_rows
    }

    step9_flag: dict[str, dict[str, str]] = {}
    for r in step9_flag_rows:
        k = (r.get("Company Name") or "").strip().lower()
        if k:
            step9_flag[k] = r

    master_rows, master_fields = build_master(
        scored, retained_keys, step9_flag, step9_clean_keys
    )
    _write_csv(OUT_MASTER, master_rows, master_fields)

    # Shortlist: Step8-retained only, outreach helpers
    short_src: list[dict[str, str]] = []
    for r in step8_clean:
        rr = dict(r)
        ck = (rr.get("Company Name") or "").strip().lower()
        if ck in step9_flag:
            rr["Step9_QA_Tags"] = step9_flag[ck].get("Step9_QA_Tags", "")
            rr["Step9_QA_Flag_Count"] = step9_flag[ck].get("Step9_QA_Flag_Count", "")
            if "calibration_review" in (rr["Step9_QA_Tags"] or "").lower():
                rr["Step9_Outreach_Note"] = "QA flagged — review tags before outreach"
            else:
                rr["Step9_Outreach_Note"] = "QA flagged"
        elif ck in step9_clean_keys:
            rr["Step9_QA_Tags"] = ""
            rr["Step9_QA_Flag_Count"] = "0"
            rr["Step9_Outreach_Note"] = "QA clean"
        else:
            rr["Step9_QA_Tags"] = ""
            rr["Step9_QA_Flag_Count"] = ""
            rr["Step9_Outreach_Note"] = "Step9 file missing row (re-run qa_icp_step9)"
        short_src.append(rr)

    short_deduped = dedupe_shortlist_rows(short_src)
    _write_csv(OUT_SHORT, short_deduped, SHORTLIST_COLS)

    # Rejects: normalized reason codes
    rej_out: list[dict[str, str]] = []
    rfields = [
        "Company Name",
        "City",
        "Industry Segment",
        "Website",
        "Revenue Band",
        "CIN",
        "ICP_Blended_Weighted_Score",
        "ICP_Weighted_Score",
        "Reject_Phase",
        "Reject_Reason_Codes",
        "Dedupe_Winner_Company",
        "Dedupe_Basis",
    ]
    for r in step8_rej:
        rej_out.append(
            {
                "Company Name": r.get("Company Name", ""),
                "City": r.get("City", ""),
                "Industry Segment": r.get("Industry Segment", ""),
                "Website": r.get("Website", ""),
                "Revenue Band": r.get("Revenue Band", ""),
                "CIN": r.get("CIN", ""),
                "ICP_Blended_Weighted_Score": r.get("ICP_Blended_Weighted_Score", ""),
                "ICP_Weighted_Score": r.get("ICP_Weighted_Score", ""),
                "Reject_Phase": "step8",
                "Reject_Reason_Codes": r.get("Step8_Reject_Tags", ""),
                "Dedupe_Winner_Company": r.get("Step8_Dedupe_Winner_Company", ""),
                "Dedupe_Basis": r.get("Step8_Dedupe_Basis", ""),
            }
        )
    _write_csv(OUT_REJECT, rej_out, rfields)

    # --- Summary ---
    raw_drhp = len(_read_csv(F_DRHP)) if F_DRHP.exists() else 0
    raw_merged = len(_read_csv(F_MERGED)) if F_MERGED.exists() else 0
    raw_dsir = len(_read_csv(F_DATA_DSIR)) if F_DATA_DSIR.exists() else 0
    raw_sum_lines = raw_drhp + raw_merged + raw_dsir

    n_scored = len(scored)
    n_retained = len(step8_clean)
    n_rejected = len(step8_rej)
    n_short_final = len(short_deduped)
    n_step9_clean = len(step9_clean_rows)
    n_step9_flag = len(step9_flag_rows)

    sector_ctr = Counter(
        (r.get("Industry Segment") or "unknown").strip() or "unknown"
        for r in master_rows
    )
    rev_ctr = Counter(
        (r.get("Revenue Band") or "unknown").strip() or "unknown"
        for r in master_rows
    )
    cov_master = criterion_coverage_pct(master_rows)
    cov_short = criterion_coverage_pct(short_deduped)

    tail_master = tailwind_token_counts(master_rows, 18)
    tail_short = tailwind_token_counts(short_deduped, 18)

    lines = [
        "ICP final outputs summary",
        "========================",
        "",
        "1) Input volumes (source files feeding merge_unified_icp)",
        f"    drhp_companies_specified_format.csv rows: {raw_drhp}",
        f"    final_merged_dsir_promoters.csv rows:        {raw_merged}",
        f"    data_with_dsir_recognition.csv rows:         {raw_dsir}",
        f"    Sum of above three (includes overlaps):      {raw_sum_lines}",
        "",
        "2) Deduped scored universe (merge_unified_icp + Step 7 output)",
        f"    icp_shortlist_top1000_llm_scored.csv rows: {n_scored}",
        "",
        "3) Step 8 hard filters",
        f"    Retained (icp_shortlist_step8_clean.csv): {n_retained}",
        f"    Rejected (icp_step8_rejects.csv):        {n_rejected}",
        "",
        "4) Step 9 QA split (runs on Step 8 retained cohort)",
        f"    Flagged (icp_step9_flagged.csv): {n_step9_flag}",
        f"    Clean   (icp_step9_clean.csv):   {n_step9_clean}",
        "",
        "5) Final deliverables (this run)",
        f"    icp_final_master.csv rows:    {len(master_rows)}",
        f"    icp_final_shortlist.csv rows: {n_short_final} (cap={SHORTLIST_CAP}, deduped)",
        f"    icp_final_rejects.csv rows:   {len(rej_out)}",
        "",
        "6) Retention funnel",
        f"    Scored universe -> Step8 retained: {n_retained}/{n_scored} = {round(100*n_retained/n_scored,1)}%" if n_scored else "",
        f"    Step8 retained -> Step9 clean:      {n_step9_clean}/{n_retained} = {round(100*n_step9_clean/n_retained,1)}%" if n_retained else "",
        "",
        "7) Industry Segment (normalized) — icp_final_master cohort",
    ]
    for name, cnt in sector_ctr.most_common(25):
        lines.append(f"    {cnt:4d}  {name}")

    lines += [
        "",
        "8) Revenue Band — icp_final_master cohort",
    ]
    for name, cnt in rev_ctr.most_common(22):
        lines.append(f"    {cnt:4d}  {name[:76]}")

    lines += [
        "",
        "9) Top tailwind tokens (pipe-split Tailwinds) — master",
    ]
    for tok, cnt in tail_master:
        lines.append(f"    {cnt:4d}  {tok[:70]}")

    lines += [
        "",
        "10) Top tailwind tokens — icp_final_shortlist cohort",
    ]
    for tok, cnt in tail_short:
        lines.append(f"    {cnt:4d}  {tok[:70]}")

    lines += [
        "",
        "11) ICP criterion coverage % (binary columns = 1) — master cohort",
    ]
    for k in sorted(cov_master.keys()):
        lines.append(f"    {k}: {cov_master[k]}%")

    lines += [
        "",
        "12) ICP criterion coverage % — icp_final_shortlist cohort",
    ]
    for k in sorted(cov_short.keys()):
        lines.append(f"    {k}: {cov_short[k]}%")

    lines += [
        "",
        "Artifacts:",
        f"    {OUT_MASTER.name}",
        f"    {OUT_SHORT.name}",
        f"    {OUT_REJECT.name}",
        f"    {OUT_SUMMARY.name}",
        "",
    ]

    OUT_SUMMARY.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
