"""
LLM-assisted ICP scoring (Anthropic) on the ranked shortlist, blended with rule-based C1–C6 flags.

Reads: icp_shortlist_top{N}.csv (default top 1000)
Writes: icp_shortlist_top{N}_llm_scored.csv (UTF-8 BOM)

Environment:
  ANTHROPIC_API_KEY — required for LLM calls (not needed with --rules-only)
  ANTHROPIC_MODEL — optional (default claude-haiku-4-5-20251001)
  BLEND_ALPHA — optional 0..1 (default 0.6 → 60% LLM normalized score, 40% rule binary)

Insufficient Anthropic credits / balance:
  Add billing or credits at https://console.anthropic.com/ — or run with --rules-only (no API; scores = rule flags only, alpha forced to 0).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

from anthropic import Anthropic

from merge_unified_icp import ICP_WEIGHTS

ROOT = Path(__file__).resolve().parent

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
RULE_KEYS = [
    "C1_Manufacturer",
    "C2_India_Based",
    "C3_Differentiation",
    "C4_Technical_DM",
    "C5_Tailwinds",
    "C6_Growth_Signals",
]

TRUNCATE = 480

SYSTEM = """You score Indian B2B / industrial companies for ICP fit. For each company you output integers 0-5 per criterion.

Criteria (higher = stronger fit):
- c1: Manufacturer or substantive industrial producer (plants, OEM, materials, hardware, APIs — not pure trading/reselling without manufacturing).
- c2: India-based operations (registered office, manufacturing, or clear India HQ; penalize offshore-only shells).
- c3: Differentiation evidence (DSIR, patents, proprietary process, strong certifications like ISO in R&D/manufacturing context, recognized labs — not generic mentions alone).
- c4: Identifiable technical / executive decision-makers (named promoters, CTO/MD with technical depth in text — not empty or generic DRHP boilerplate only).
- c5: Sector or macro tailwinds aligned with growth (policy, demand, listed pipeline relevance).
- c6: Growth signals (IPO/pre-IPO, capacity expansion, capex, hiring, new facilities, certifications underway).

Use only the provided text. If data is missing or boilerplate, score conservatively (0-2). Never invent facts not supported by the snippets.

Respond with ONLY valid JSON: an array of objects, one per company, in the same order as input.
Each object: {"company_name": "<exact name>", "c1": int, "c2": int, "c3": int, "c4": int, "c5": int, "c6": int, "note": "<=200 chars citing strongest signals>"}
Scores must be integers 0-5."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Blend Anthropic LLM scores with rule-based ICP flags.")
    p.add_argument(
        "--input",
        type=Path,
        default=ROOT / "icp_shortlist_top1000.csv",
        help="Shortlist CSV from merge_unified_icp.py",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: <input stem>_llm_scored.csv)",
    )
    p.add_argument("--batch-size", type=int, default=10, metavar="N", help="Companies per API call")
    p.add_argument(
        "--blend-alpha",
        type=float,
        default=float(os.environ.get("BLEND_ALPHA", "0.6")),
        help="Weight on LLM (0-1); rest is rule binary. Default 0.6 or BLEND_ALPHA env.",
    )
    p.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    p.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="JSONL append checkpoint for resume (default: <input stem>_llm_checkpoint.jsonl)",
    )
    p.add_argument("--sleep", type=float, default=0.4, help="Seconds between API calls")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only first N rows (0 = all)",
    )
    p.add_argument(
        "--rules-only",
        action="store_true",
        help="Do not call Anthropic. LLM columns are 0; blend uses only rule flags (alpha=0). Use when out of credits.",
    )
    return p.parse_args()


def _trunc(s: str, n: int = TRUNCATE) -> str:
    s = (s or "").strip().replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def row_to_payload_min(row: dict[str, str]) -> dict[str, str]:
    """Compact payload for the model (rule hints as short notes)."""
    notes = [
        f"C1_rule={row.get('C1_Manufacturer')}:{_trunc(row.get('C1_Notes', ''), 120)}",
        f"C2_rule={row.get('C2_India_Based')}:{_trunc(row.get('C2_Notes', ''), 120)}",
        f"C3_rule={row.get('C3_Differentiation')}:{_trunc(row.get('C3_Notes', ''), 120)}",
        f"C4_rule={row.get('C4_Technical_DM')}:{_trunc(row.get('C4_Notes', ''), 120)}",
        f"C5_rule={row.get('C5_Tailwinds')}:{_trunc(row.get('C5_Notes', ''), 120)}",
        f"C6_rule={row.get('C6_Growth_Signals')}:{_trunc(row.get('C6_Notes', ''), 120)}",
    ]
    return {
        "company_name": row.get("Company Name", "").strip(),
        "city": _trunc(row.get("City", "")),
        "industry_segment": _trunc(row.get("Industry Segment", "")),
        "industry_raw": _trunc(row.get("Industry Segment (raw)", "")),
        "website": _trunc(row.get("Website", "")),
        "revenue_band": _trunc(row.get("Revenue Band", "")),
        "differentiation": _trunc(row.get("Differentiation Evidence", "")),
        "promoter": _trunc(row.get("Promoter/Decision-Maker", "")),
        "growth": _trunc(row.get("Growth Signals", "")),
        "tailwinds": _trunc(row.get("Tailwinds", "")),
        "automated_rule_hints": _trunc(" | ".join(notes), 900),
    }


def extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected JSON array")
    return data


def clamp_int_score(x: object) -> int:
    try:
        v = int(round(float(x)))
    except (TypeError, ValueError):
        v = 0
    return max(0, min(5, v))


def align_batch_scores(
    batch_rows: list[dict[str, str]],
    llm_parsed: list[dict],
) -> list[dict]:
    """Map model output to each row by company_name (order-safe)."""
    by_name: dict[str, dict] = {}
    for item in llm_parsed:
        if not isinstance(item, dict):
            continue
        key = str(item.get("company_name", "")).strip()
        if key:
            by_name[key] = item
    aligned: list[dict] = []
    for r in batch_rows:
        name = r.get("Company Name", "").strip()
        aligned.append(by_name.get(name, {}))
    return aligned


def call_llm_batch(
    client: Anthropic,
    model: str,
    batch: list[dict[str, str]],
) -> list[dict]:
    user_obj = {"companies": [row_to_payload_min(r) for r in batch]}
    user_txt = json.dumps(user_obj, ensure_ascii=False)

    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_txt}],
    )
    if not msg.content or msg.content[0].type != "text":
        raise RuntimeError("Unexpected Anthropic response shape")
    raw = msg.content[0].text
    return extract_json_array(raw)


def load_checkpoint(path: Path) -> dict[str, dict]:
    by_name: dict[str, dict] = {}
    if not path.exists():
        return by_name
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            name = rec.get("company_name", "")
            if name:
                by_name[name] = rec
    return by_name


def append_checkpoint(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def blend_scores(
    rules: list[int],
    llm: list[int],
    alpha: float,
) -> tuple[list[float], list[float]]:
    """Return (blended_norm per criterion 0-1, blended_0_5 per criterion)."""
    alpha = max(0.0, min(1.0, alpha))
    norms: list[float] = []
    out5: list[float] = []
    for r, l in zip(rules, llm, strict=True):
        l5 = max(0.0, min(5.0, float(l)))
        llm_norm = l5 / 5.0
        rule_f = float(max(0, min(1, r)))
        bn = alpha * llm_norm + (1.0 - alpha) * rule_f
        norms.append(bn)
        out5.append(round(bn * 5.0, 2))
    return norms, out5


def weighted_icp(norms: list[float]) -> float:
    wsum = (
        ICP_WEIGHTS["c1"] * norms[0]
        + ICP_WEIGHTS["c2"] * norms[1]
        + ICP_WEIGHTS["c3"] * norms[2]
        + ICP_WEIGHTS["c4"] * norms[3]
        + ICP_WEIGHTS["c5"] * norms[4]
        + ICP_WEIGHTS["c6"] * norms[5]
    )
    return round(100.0 * wsum, 1)


def main() -> None:
    args = parse_args()
    alpha = 0.0 if args.rules_only else max(0.0, min(1.0, args.blend_alpha))
    inp = args.input.resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    out = args.output
    if out is None:
        out = inp.with_name(f"{inp.stem}_llm_scored.csv")
    out = out.resolve()

    ck = args.checkpoint
    if ck is None:
        ck = inp.with_name(f"{inp.stem}_llm_checkpoint.jsonl")
    ck = ck.resolve()

    with inp.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames_in = reader.fieldnames or []
        rows = list(reader)

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    if args.rules_only:
        print("Rules-only mode: no API calls; blend alpha=0 (100% rule binary).")
        zero_score = {"c1": 0, "c2": 0, "c3": 0, "c4": 0, "c5": 0, "c6": 0}
        cached: dict[str, dict] = {}
        for row in rows:
            name = row.get("Company Name", "").strip()
            if not name:
                continue
            cached[name] = {
                "company_name": name,
                **zero_score,
                "note": "rules_only_no_llm_balance",
            }
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit(
                "Set ANTHROPIC_API_KEY, or use --rules-only if you have no API balance."
            )

        client = Anthropic(api_key=api_key)
        cached = load_checkpoint(ck)

        pending: list[tuple[int, dict[str, str]]] = []
        for i, row in enumerate(rows):
            name = row.get("Company Name", "").strip()
            if name in cached:
                continue
            pending.append((i, row))

        bs = max(1, args.batch_size)
        batches: list[list[tuple[int, dict[str, str]]]] = []
        cur: list[tuple[int, dict[str, str]]] = []
        for item in pending:
            cur.append(item)
            if len(cur) >= bs:
                batches.append(cur)
                cur = []
        if cur:
            batches.append(cur)

        for batch in batches:
            idx_rows = batch
            try:
                llm_parsed = call_llm_batch(client, args.model, [r for _, r in idx_rows])
            except Exception as e:
                print(f"Batch failed ({e}); retrying one company at a time.")
                llm_parsed = []
                for _, r in idx_rows:
                    try:
                        one = call_llm_batch(client, args.model, [r])
                        llm_parsed.extend(one)
                    except Exception as e2:
                        print(f"  Skip {r.get('Company Name')!r}: {e2}")
                        llm_parsed.append(
                            {
                                "company_name": r.get("Company Name", ""),
                                "c1": 0,
                                "c2": 0,
                                "c3": 0,
                                "c4": 0,
                                "c5": 0,
                                "c6": 0,
                                "note": "llm_error",
                            }
                        )
                    time.sleep(args.sleep)

            batch_rows_only = [r for _, r in idx_rows]
            if len(llm_parsed) != len(idx_rows):
                print(
                    f"Warning: batch length mismatch got {len(llm_parsed)} expected {len(idx_rows)}; aligning by name."
                )
            aligned = align_batch_scores(batch_rows_only, llm_parsed)
            for (_, row), rec in zip(idx_rows, aligned, strict=True):
                name = row.get("Company Name", "").strip()
                rec = rec or {}
                out_rec = {
                    "company_name": name,
                    "c1": clamp_int_score(rec.get("c1")),
                    "c2": clamp_int_score(rec.get("c2")),
                    "c3": clamp_int_score(rec.get("c3")),
                    "c4": clamp_int_score(rec.get("c4")),
                    "c5": clamp_int_score(rec.get("c5")),
                    "c6": clamp_int_score(rec.get("c6")),
                    "note": str(rec.get("note", ""))[:500],
                }
                append_checkpoint(ck, out_rec)
                cached[name] = out_rec
            time.sleep(args.sleep)

    model_label = "rules_only" if args.rules_only else args.model

    extra_cols = [
        "LLM_C1",
        "LLM_C2",
        "LLM_C3",
        "LLM_C4",
        "LLM_C5",
        "LLM_C6",
        "LLM_Note",
        "Blended_C1",
        "Blended_C2",
        "Blended_C3",
        "Blended_C4",
        "Blended_C5",
        "Blended_C6",
        "ICP_Blended_Weighted_Score",
        "LLM_Blend_Alpha",
        "LLM_Model",
    ]
    out_fields = list(fieldnames_in) + [c for c in extra_cols if c not in fieldnames_in]

    out_rows: list[dict[str, str]] = []
    for row in rows:
        name = row.get("Company Name", "").strip()
        rules = [int(row.get(k, "0") or 0) for k in RULE_KEYS]
        cr = cached.get(name, {})
        llm = [
            int(cr.get("c1", 0)),
            int(cr.get("c2", 0)),
            int(cr.get("c3", 0)),
            int(cr.get("c4", 0)),
            int(cr.get("c5", 0)),
            int(cr.get("c6", 0)),
        ]
        norms, b5 = blend_scores(rules, llm, alpha)
        wtd = weighted_icp(norms)
        enriched = dict(row)
        for i in range(6):
            enriched[f"LLM_C{i + 1}"] = str(llm[i])
            enriched[f"Blended_C{i + 1}"] = str(b5[i])
        enriched["LLM_Note"] = str(cr.get("note", ""))
        enriched["ICP_Blended_Weighted_Score"] = str(wtd)
        enriched["LLM_Blend_Alpha"] = str(alpha)
        enriched["LLM_Model"] = model_label
        out_rows.append(enriched)

    out_rows.sort(
        key=lambda r: (-float(r.get("ICP_Blended_Weighted_Score", "0") or 0), r["Company Name"].upper())
    )

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)

    print(f"Wrote {out.name}: {len(out_rows)} rows (blend alpha={alpha}, model={model_label}).")


if __name__ == "__main__":
    main()
