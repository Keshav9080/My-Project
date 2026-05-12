"""
Step 8: Hard pre-filters + dedupe on icp_shortlist_top1000_llm_scored.csv

Outputs:
  - icp_shortlist_step8_clean.csv
  - icp_step8_rejects.csv (with Step8_Reject_Tags)

Uses dynamic ancillary-domain detection (frequent hosts across file ≈ RTA/bankers)
plus static park/trading/revenue rules.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_INP = ROOT / "icp_shortlist_top1000_llm_scored.csv"
OUT_CLEAN = ROOT / "icp_shortlist_step8_clean.csv"
OUT_REJECT = ROOT / "icp_step8_rejects.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 8 hard filters + dedupe for ICP shortlist.")
    p.add_argument("--input", type=Path, default=DEFAULT_INP, help="Input CSV path")
    p.add_argument("--out-clean", type=Path, default=OUT_CLEAN)
    p.add_argument("--out-rejects", type=Path, default=OUT_REJECT)
    p.add_argument(
        "--ancillary-freq",
        type=int,
        default=None,
        metavar="N",
        help="Treat hosts appearing in ≥N rows as ancillary (default: auto from row count). Lower ⇒ stricter website filter.",
    )
    return p.parse_args()

# CIN: L/U + 5 digits + 2 state chars + 4 year + 3 type + 6 seeds
CIN_RE = re.compile(
    r"\b([LUlu]\d{5}[A-Za-z]{2}\d{4}[A-Za-z]{3}\d{6})\b",
)

# Always treat as non-corporate (IPO/RTA/brokerage patterns; expand as needed)
STATIC_ANCILLARY_SUFFIXES = (
    "bigshareonline.com",
    "kfintech.com",
    "linkintime.co.in",
    "linkintime.in",
    "skylinerta.com",
    "maashitla.com",
    "narnolia.com",
    "hemsecurities.com",
    "mufg.com",
    "kreocapital.com",
    "inventuremerchantbanker.com",
    "khambattasecurities.com",
    "cumulativecapital.group",
    "purvashare.com",
    "nirbhaycapital.com",
    "corporatemakers.in",
    "ifinservices.in",
    "finaaxcapital.com",
    "beelinebroking.com",
    "gyrcapitaladvisors.com",
    "finshore.biz",
    "kfintech.co.in",
    "myipoclub.com",
)

PARKED_RE = re.compile(
    r"under\s+construction|coming\s+soon|domain\s+(is\s+)?(for\s+)?sale|"
    r"parked\s+domain|buy\s+this\s+domain|website\s+temporarily\s+unavailable|"
    r"been parked",
    re.I,
)

TRADING_NAME_RE = re.compile(
    r"\b(traders?|distributors?|importers?|exporters?|import-?export|import/export|"
    r"wholesaler?s?|stockist|trading\s+company|trading\s+house)\b",
    re.I,
)

# Revenue: extract numbers; treat lakh as /100 Cr approx
NUM_PAIR_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?)\s*"
    r"([\d,.]+)\s*(?:–|-|to)\s*([\d,.]+)\s*(?:cr|crore)",
    re.I,
)
NUM_SINGLE_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?)\s*([\d,.]+)\s*(?:cr|crore)",
    re.I,
)
NUM_LAKH_RE = re.compile(
    r"(?:₹|rs\.?)?\s*([\d,.]+)\s*(?:lakh|lakhs|lac)\b",
    re.I,
)

REV_UNKNOWN_FRAGMENTS = (
    "pattern not matched",
    "unknown",
    "not assessed",
    "extract from restated",
)


def _parse_num(s: str) -> float:
    return float(re.sub(r",", "", s.strip()))


def max_revenue_crore(rev: str) -> float | None:
    """Upper-bound revenue in ₹ Cr if parseable; None if unknown/unparsed."""
    if not rev or not rev.strip():
        return None
    low = rev.lower()
    if any(x in low for x in REV_UNKNOWN_FRAGMENTS):
        return None

    m = NUM_PAIR_RE.search(rev)
    if m:
        try:
            a, b = _parse_num(m.group(1)), _parse_num(m.group(2))
            return max(a, b)
        except ValueError:
            pass
    m = NUM_SINGLE_RE.search(rev)
    if m:
        try:
            return _parse_num(m.group(1))
        except ValueError:
            pass
    m = NUM_LAKH_RE.search(rev)
    if m:
        try:
            return _parse_num(m.group(1)) / 100.0
        except ValueError:
            pass
    return None


def norm_host(url_token: str) -> str:
    t = url_token.strip()
    if not t:
        return ""
    if "://" not in t:
        t = "http://" + t
    try:
        netloc = urlparse(t).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def split_website_field(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[;,\|]+", raw)
    return [p.strip() for p in parts if p.strip()]


def extract_hosts(website_field: str) -> list[str]:
    hosts: list[str] = []
    for p in split_website_field(website_field):
        h = norm_host(p)
        if h:
            hosts.append(h)
    return hosts


def is_ancillary_host(host: str, ancillary: frozenset[str]) -> bool:
    h = host.lower()
    if h in ancillary:
        return True
    for suf in STATIC_ANCILLARY_SUFFIXES:
        if h == suf or h.endswith("." + suf):
            return True
    return False


def corporate_hosts_for_row(
    website_field: str, ancillary: frozenset[str]
) -> list[str]:
    return [h for h in extract_hosts(website_field) if not is_ancillary_host(h, ancillary)]


def extract_cin(blob: str) -> str:
    m = CIN_RE.search(blob or "")
    return m.group(1).upper() if m else ""


_NAME_TOKENS = re.compile(
    r"(?:Mr\.|Mrs\.|Ms\.|Dr\.|Shri|Smt\.)\s+([A-Za-z][A-Za-z.'\-]+(?:\s+[A-Za-z][A-Za-z.'\-]+)*)",
)


def promoter_dedupe_key(promoter: str) -> str:
    if not promoter or len(promoter) < 8:
        return ""
    toks = [m.group(1).strip().lower() for m in _NAME_TOKENS.finditer(promoter)]
    toks = sorted(set(toks))
    if not toks:
        return ""
    return "|".join(toks[:12])


def union_find_union(parent: list[int], i: int, j: int) -> None:
    pi, pj = _union_find_find(parent, i), _union_find_find(parent, j)
    if pi != pj:
        parent[pi] = pj


def _union_find_find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def main() -> None:
    args = parse_args()
    inp = args.input.resolve()
    out_clean = args.out_clean.resolve()
    out_reject = args.out_rejects.resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    with inp.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        base_fields = reader.fieldnames or []
        rows = list(reader)

    total_in = len(rows)
    host_counts: Counter[str] = Counter()
    for r in rows:
        for h in extract_hosts(r.get("Website", "")):
            host_counts[h] += 1

    # Hosts seen in many rows → RTA / shared service pages
    if args.ancillary_freq is not None:
        freq_cut = max(3, args.ancillary_freq)
    else:
        freq_cut = max(7, min(22, (total_in + 20) // 35))
    dynamic_ancillary = {h for h, c in host_counts.items() if c >= freq_cut}
    ancillary = frozenset(dynamic_ancillary)

    enriched: list[tuple[dict[str, str], list[str]]] = []
    for r in rows:
        tags: list[str] = []
        name = (r.get("Company Name") or "").strip()
        web = r.get("Website", "") or ""
        corp = corporate_hosts_for_row(web, ancillary)
        blob = " ".join(
            [
                web,
                r.get("City", "") or "",
                r.get("Growth Signals", "") or "",
                r.get("Differentiation Evidence", "") or "",
            ]
        )
        if PARKED_RE.search(blob):
            tags.append("website_parked_or_placeholder_text")

        if not extract_hosts(web):
            tags.append("no_website_url")
        elif not corp:
            tags.append("website_only_ancillary_domains")

        if name and TRADING_NAME_RE.search(name):
            tags.append("trading_import_export_name_keyword")

        rc = max_revenue_crore(r.get("Revenue Band", "") or "")
        if rc is not None and rc > 500:
            tags.append("revenue_over_500_cr")

        cin = (r.get("CIN") or "").strip().upper() or extract_cin(
            " ".join(
                [
                    name,
                    r.get("Promoter/Decision-Maker") or "",
                    r.get("City") or "",
                ]
            )
        )

        r = dict(r)
        r["_step8_cin"] = cin
        r["_step8_corp_hosts"] = tuple(sorted(set(corp)))
        r["_step8_promoter_key"] = promoter_dedupe_key(r.get("Promoter/Decision-Maker", "") or "")

        seen: set[str] = set()
        utags: list[str] = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                utags.append(t)
        enriched.append((r, utags))

    pass_idx = [i for i, (_, t) in enumerate(enriched) if not t]
    parent = list(range(len(pass_idx)))

    cin_map: dict[str, list[int]] = defaultdict(list)
    web_tuple_map: dict[tuple[str, ...], list[int]] = defaultdict(list)
    prom_map: dict[str, list[int]] = defaultdict(list)
    host_map: dict[str, list[int]] = defaultdict(list)

    for local_i, g in enumerate(pass_idx):
        row, _ = enriched[g]
        cin = row["_step8_cin"]
        if cin:
            cin_map[cin].append(local_i)
        ch = row["_step8_corp_hosts"]
        if ch:
            web_tuple_map[ch].append(local_i)
            for h in ch:
                host_map[h].append(local_i)
        pk = row["_step8_promoter_key"]
        if pk and pk.count("|") >= 1:
            prom_map[pk].append(local_i)

    for group in cin_map.values():
        for a in range(1, len(group)):
            union_find_union(parent, group[0], group[a])
    for group in web_tuple_map.values():
        for a in range(1, len(group)):
            union_find_union(parent, group[0], group[a])
    # Same corporate hostname appearing on multiple rows (dedupe “by website URL”)
    for group in host_map.values():
        if len(group) < 2:
            continue
        for a in range(1, len(group)):
            union_find_union(parent, group[0], group[a])
    for group in prom_map.values():
        for a in range(1, len(group)):
            union_find_union(parent, group[0], group[a])

    comp_winners: dict[int, int] = {}
    comp_members: dict[int, list[int]] = defaultdict(list)
    for local_i in range(len(pass_idx)):
        root = _union_find_find(parent, local_i)
        comp_members[root].append(local_i)

    def score_key(local_i: int) -> tuple[float, str]:
        g = pass_idx[local_i]
        row, _ = enriched[g]
        try:
            sc = float(row.get("ICP_Blended_Weighted_Score") or 0)
        except ValueError:
            sc = 0.0
        return (sc, (row.get("Company Name") or "").upper())

    for root, members in comp_members.items():
        members_sorted = sorted(members, key=score_key, reverse=True)
        comp_winners[root] = members_sorted[0]

    duplicate_local: set[int] = set()
    dup_meta: dict[int, tuple[str, str]] = {}
    for root, members in comp_members.items():
        win = comp_winners[root]
        wrow, _ = enriched[pass_idx[win]]
        wname = wrow.get("Company Name", "")
        bases: list[str] = []
        if len(cin_map.get(wrow["_step8_cin"], [])) > 1 and wrow["_step8_cin"]:
            bases.append("cin")
        if wrow["_step8_corp_hosts"]:
            for h in wrow["_step8_corp_hosts"]:
                if len(host_map.get(h, [])) > 1:
                    bases.append("website_host")
                    break
        if len(prom_map.get(wrow["_step8_promoter_key"], [])) > 1 and wrow["_step8_promoter_key"]:
            bases.append("promoter")
        basis = "+".join(bases) if bases else "cluster"
        for m in members:
            if m != win:
                duplicate_local.add(m)
                dup_meta[m] = (wname, basis)

    clean_rows: list[dict[str, str]] = []
    reject_rows: list[dict[str, str]] = []

    for local_i in range(len(pass_idx)):
        if local_i in duplicate_local:
            continue
        g = pass_idx[local_i]
        row, _ = enriched[g]
        out = {k: v for k, v in row.items() if not k.startswith("_step8_")}
        clean_rows.append(out)

    for g, (row, utags) in enumerate(enriched):
        if utags:
            out = {k: v for k, v in row.items() if not k.startswith("_step8_")}
            out["Step8_Reject_Tags"] = "|".join(utags)
            out["Step8_Dedupe_Winner_Company"] = ""
            out["Step8_Dedupe_Basis"] = ""
            reject_rows.append(out)

    for local_i in duplicate_local:
        g = pass_idx[local_i]
        row, _ = enriched[g]
        out = {k: v for k, v in row.items() if not k.startswith("_step8_")}
        winner, basis = dup_meta[local_i]
        tags = ["duplicate_dedupe", f"basis:{basis}"]
        out["Step8_Reject_Tags"] = "|".join(tags)
        out["Step8_Dedupe_Winner_Company"] = winner
        out["Step8_Dedupe_Basis"] = basis
        reject_rows.append(out)

    extra = ["Step8_Reject_Tags", "Step8_Dedupe_Winner_Company", "Step8_Dedupe_Basis"]
    rej_fields = [c for c in base_fields if c] + [
        c for c in extra if c not in (base_fields or [])
    ]

    def sort_key_company(rd: dict[str, str]) -> str:
        return (rd.get("Company Name") or "").upper()

    clean_rows.sort(key=sort_key_company)
    reject_rows.sort(key=sort_key_company)

    with out_clean.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=base_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(clean_rows)

    with out_reject.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rej_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(reject_rows)

    tag_tally: Counter[str] = Counter()
    for rj in reject_rows:
        for t in (rj.get("Step8_Reject_Tags") or "").split("|"):
            if t.strip():
                tag_tally[t.strip()] += 1

    eliminated = len(reject_rows)
    retained = len(clean_rows)
    summary_lines = [
        f"Step 8 ICP clean",
        f"input_file={inp.name}",
        f"total_input={total_in}",
        f"total_retained={retained}",
        f"total_eliminated={eliminated}",
        f"freq_ancillary_cut={freq_cut}",
        f"dynamic_ancillary_hosts={len(dynamic_ancillary)}",
        "",
        "Reject tag counts (rows may have multiple tags only when noted; dedupe rows use compound tags):",
    ]
    for k, v in sorted(tag_tally.items(), key=lambda x: -x[1]):
        summary_lines.append(f"  {k}={v}")
    summary_text = "\n".join(summary_lines) + "\n"

    summary_path = out_clean.with_name("icp_step8_summary.txt")
    summary_path.write_text(summary_text, encoding="utf-8")

    print(
        f"Step 8 summary: input={total_in}, retained={retained}, eliminated={eliminated}, "
        f"freq_ancillary_cut={freq_cut} (dynamic ancillary hosts={len(dynamic_ancillary)})"
    )
    if total_in >= 3500 and not (500 <= eliminated <= 800):
        print(
            "Note: Target ~500-800 eliminated fits ~3.5k-4k-row shortlists; "
            "tune --ancillary-freq if your count is off."
        )
    elif total_in < 1200:
        print(
            f"Note: input={total_in} rows (small list); eliminated={eliminated}. "
            "Expect ~500-800 eliminations when input is ~3.5k-4k with the same rules."
        )
    for k, v in sorted(tag_tally.items(), key=lambda x: -x[1]):
        print(f"  reject_tag {k}: {v}")
    print(f"Wrote {out_clean.name}, {out_reject.name}, {summary_path.name}")


if __name__ == "__main__":
    main()
