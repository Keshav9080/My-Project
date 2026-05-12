"""
Merge data_with_dsir_recognition.csv, final_merged_dsir_promoters.csv,
and drhp_companies_specified_format.csv into one deduplicated table with
ICP tags C1–C6, optional weighted composite (ICP_WEIGHTS), ranked by fit.

Outputs:
  - unified_icp_master.csv (UTF-8 BOM for Excel), all deduped rows ranked by fit
  - icp_shortlist_top{N}.csv — top N rows (default N=1000), same columns
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent

F_DATA = ROOT / "data_with_dsir_recognition.csv"
F_MERGED = ROOT / "final_merged_dsir_promoters.csv"
F_DRHP = ROOT / "drhp_companies_specified_format.csv"
OUT = ROOT / "unified_icp_master.csv"

# Step 7 scoring matrix: weights on binary C1–C6 (0/1). Scale to 0–5 per criterion via LLM separately if needed.
ICP_WEIGHTS = {
    "c1": 0.20,  # Manufacturer
    "c2": 0.10,  # India-based
    "c3": 0.20,  # Differentiation
    "c4": 0.15,  # Technical DM
    "c5": 0.15,  # Tailwinds
    "c6": 0.20,  # Growth signals
}

CIN_RE = re.compile(
    r"\b([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b",
    re.I,
)

GENERIC_INDUSTRY = frozenset(
    {
        "see drhp ‘industry / business overview’ section.",
        "see drhp 'industry / business overview' section.",
    }
)
GENERIC_PROMOTER = frozenset(
    {
        "",
        "promoter: drhp",
        "promoter names not machine-extracted from sampled drhp pages; verify in ipo prospectus or linkedin.",
        "could not download drhp pdf/zip for promoter extraction.",
        "drhp pdf could not be downloaded.",
        "see drhp ‘promoters’ and ‘management’ sections.",
        "see drhp 'promoters' and 'management' sections.",
        "no bse drhp link found for promoter extraction.",
    }
)


def slug_tags(text: str, max_tags: int = 12) -> str:
    """Normalize evidence tags: lowercase pipe-separated tokens."""
    if not text or not text.strip():
        return ""
    raw = re.split(r"[;,|/\n]+", text.lower())
    seen: list[str] = []
    for t in raw:
        t = re.sub(r"\s+", "_", t.strip(" ._"))
        if len(t) >= 3 and t not in seen:
            seen.append(t)
    return "|".join(seen[:max_tags])


def normalize_company_key(name: str) -> str:
    if not name:
        return ""
    s = name.upper().replace("&", " AND ")
    s = re.sub(
        r"\b(LIMITED|LTD|PRIVATE|PVT\.?|THE|INDIA|CO\.|COMPANY)\b",
        " ",
        s,
    )
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def extract_cin(blob: str) -> str | None:
    if not blob:
        return None
    m = CIN_RE.search(blob.upper())
    return m.group(1).upper() if m else None


def merge_text_best(*parts: str, min_len: int = 4) -> str:
    best = ""
    for p in parts:
        if not p:
            continue
        p = str(p).strip()
        if len(p) >= min_len and len(p) > len(best):
            # Prefer structured short city over DRHP wall-of-text when similar length conflict later
            best = p
    return best


def pick_city(*candidates: str) -> str:
    """Prefer concise location over embedded DRHP prose."""
    scored: list[tuple[int, str]] = []
    for c in candidates:
        if not c or not str(c).strip():
            continue
        s = " ".join(str(c).split())
        low = s.lower()
        score = 0
        if len(s) <= 60:
            score += 80
        elif len(s) <= 120:
            score += 50
        else:
            score -= len(s) // 10
        if "draft red herring" in low or "sebi icdr" in low:
            score -= 120
        if "registered office" in low and len(s) > 200:
            score -= 40
        if INDIA_LOC_HINTS.search(s):
            score += 30
        scored.append((score, s))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[0][1]
    if len(top) > 150:
        first = top.split(".")[0].strip()
        if 8 <= len(first) <= 150:
            return first
    return top[:280] + ("..." if len(top) > 280 else "")


def normalize_revenue_band(s: str) -> str:
    if not s or not str(s).strip():
        return "unknown"
    t = str(s).strip()
    low = t.lower()
    if "unknown" in low or "pattern not matched" in low or "extract from restated" in low:
        # Still try to parse numbers
        pass
    # Already a band like ₹100–200 Cr
    if "₹" in t and ("cr" in low or "crore" in low):
        return re.sub(r"\s+", " ", t)
    # Lakhs → approximate crore band label
    m = re.search(
        r"([\d,]+(?:\.\d+)?)\s*(?:lakhs?|lac\b)",
        low.replace(",", ""),
    )
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            cr = val / 100.0
            if cr < 5:
                return "₹0–5 Cr (approx from lakhs)"
            if cr < 25:
                return "₹5–25 Cr (approx from lakhs)"
            if cr < 100:
                return "₹25–100 Cr (approx from lakhs)"
            return "₹100+ Cr (approx from lakhs)"
        except ValueError:
            pass
    m2 = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:crores?|cr\.?)\b", low)
    if m2:
        return f"₹{m2.group(1)} Cr (stated)"
    return re.sub(r"\s+", " ", t)[:80]


INDUSTRY_MAP = [
    (re.compile(r"manufactur|forging|fabricat|machine|tooling|component", re.I), "Manufacturing / Industrial"),
    (re.compile(r"chemical|polymer|plastic|rubber|poly ", re.I), "Chemicals / Polymers"),
    (re.compile(r"pharma|biotech|api\b|formulation", re.I), "Pharma / Biotech"),
    (re.compile(r"defence|defense|aerospace|drone", re.I), "Defence / Aerospace"),
    (re.compile(r"software|it\b|saas|tech\b|digital", re.I), "Software / IT Services"),
    (re.compile(r"logistics|transport|freight|supply\s*chain", re.I), "Logistics / Supply chain"),
    (re.compile(r"hotel|hospitality|resort|travel|tour", re.I), "Hospitality / Travel"),
    (re.compile(r"food|agri|dairy|farm|seed", re.I), "Food / Agriculture"),
    (re.compile(r"renewable|solar|energy|power", re.I), "Energy / Renewables"),
    (re.compile(r"textile|garment|apparel|fabric", re.I), "Textiles / Apparel"),
    (re.compile(r"construction|infra|real\s*estate|developer", re.I), "Construction / Infra"),
    (re.compile(r"media|film|entertain|broadcast", re.I), "Media / Entertainment"),
    (re.compile(r"financial|capital|nbfc|broking|fintech", re.I), "Financial Services"),
]


def normalize_industry_segment(s: str) -> str:
    if not s:
        return "unknown"
    t = s.strip()
    if t.lower() in GENERIC_INDUSTRY or "see drhp" in t.lower():
        return "unspecified_see_drhp"
    for rx, label in INDUSTRY_MAP:
        if rx.search(t):
            return label
    return re.sub(r"\s+", " ", t)[:120]


INDIA_LOC_HINTS = re.compile(
    r"\b(india|bharat|maharashtra|gujarat|karnataka|tamil|telangana|"
    r"delhi|ncr|punjab|rajasthan|up\b|uttar|madhya|bihar|west bengal|"
    r"odisha|andhra|kerala|goa|assam|chhattisgarh|jharkhand|"
    r"haryana|himachal|uttarakhand|silvassa|daman|hyderabad|mumbai|"
    r"pune|bangalore|bengaluru|chennai|kolkata|ahmedabad|jaipur|"
    r"vadodara|surat|indore|noida|gurgaon|gurugram|lucknow|nagpur)\b",
    re.I,
)


def build_differentiation_evidence(dsir: str, growth: str, industry: str) -> str:
    parts: list[str] = []
    dlow = (dsir or "").lower()
    if "matched in dsir directory" in dlow:
        parts.append("dsir_directory_match")
    elif "department of scientific and industrial research" in dlow:
        parts.append("dsir_drhp_substantive_citation")
    g = (growth or "").lower()
    if "iso" in g:
        parts.append("iso_certification_mention")
    if "patent" in g or "intellectual property" in g or "ipr" in g:
        parts.append("patent_ip_mention")
    if "rd " in g or "r&d" in g or "research" in g:
        parts.append("rnd_mention")
    # De-dup preserve order
    out: list[str] = []
    for p in parts:
        if p not in out:
            out.append(p)
    return slug_tags("|".join(out)) if out else "no_structured_differentiation_tag"


def infer_tailwinds(
    industry_norm: str, growth: str, industry_raw: str, sources_combined: str
) -> str:
    tags: list[str] = []
    blob = f"{industry_norm} {growth} {industry_raw}".lower()
    src_low = sources_combined.lower()

    if (
        "drhp_companies_specified_format" in src_low
        or "final_merged_dsir_promoters" in src_low
    ):
        tags.append("bse_sme_ipo_drhp_pipeline")

    mapping = [
        ("manufacturing / industrial", "make_in_india_manufacturing"),
        ("defence / aerospace", "defence_indigenization_tailwind"),
        ("pharma / biotech", "pharma_formulations_tailwind"),
        ("energy / renewables", "energy_transition_tailwind"),
        ("software / it services", "digital_it_services_tailwind"),
        ("food / agriculture", "agri_food_processing_tailwind"),
        ("chemicals / polymers", "specialty_chemicals_tailwind"),
        ("logistics / supply chain", "logistics_infra_tailwind"),
    ]
    for needle, tag in mapping:
        if needle in industry_norm.lower():
            tags.append(tag)

    if "ipo" in blob or "fresh issue" in blob or "listed" in blob:
        tags.append("capital_markets_ipo_activity")
    if "capacity" in blob or "expansion" in blob or "capex" in blob:
        tags.append("capacity_expansion_tailwind")
    if "hiring" in blob or "recruitment" in blob:
        tags.append("workforce_scaling_signal")

    return slug_tags("|".join(tags))


def score_c1(industry_norm: str, industry_raw: str) -> tuple[int, str]:
    blob = f"{industry_norm} {industry_raw}".lower()
    mfg_kw = r"manufactur|forging|fabricat|production plant|chemical|polymer|pharma|api|oem|component|electronics hardware"
    if re.search(mfg_kw, blob):
        return 1, "manufacturer_or_industrial_segment"
    if "specialty manufacturing" in blob:
        return 1, "specialty_manufacturing_tag"
    return 0, "not_classified_manufacturer"


def score_c2(city: str) -> tuple[int, str]:
    if not city:
        return 0, "no_location"
    if INDIA_LOC_HINTS.search(city):
        return 1, "india_location_signal"
    # Many DRHP blobs mention India
    if "india" in city.lower():
        return 1, "india_mentioned_in_address_blob"
    return 0, "weak_location_signal"


def score_c3(diff_evidence_tags: str, dsir: str) -> tuple[int, str]:
    if "dsir_directory_match" in diff_evidence_tags:
        return 1, "dsir_directory_evidence"
    if "dsir_drhp_substantive_citation" in diff_evidence_tags:
        return 1, "dsir_substantive_drhp_citation"
    if "iso_certification_mention" in diff_evidence_tags or "patent_ip_mention" in diff_evidence_tags:
        return 1, "cert_or_patent_evidence"
    if dsir and "matched in dsir" in dsir.lower():
        return 1, "dsir_table_match_text"
    return 0, "no_differentiation_evidence"


def score_c4(promoter: str) -> tuple[int, str]:
    pl = promoter.strip().lower() if promoter else ""
    gen_lower = {x.lower() for x in GENERIC_PROMOTER}
    if not promoter or pl in gen_lower:
        return 0, "no_promoter_extract"
    if re.search(r"\b(Mr\.|Mrs\.|Ms\.|Dr\.)\s+[A-Z]", promoter):
        return 1, "named_individuals_in_promoter_field"
    if len(promoter) > 40 and "drhp:" in promoter.lower():
        return 1, "rich_drhp_promoter_blob"
    return 0, "weak_promoter_signal"


def score_c5(tailwinds: str) -> tuple[int, str]:
    if tailwinds and tailwinds != "":
        return 1, "sector_or_macro_tailwind_tags"
    return 0, "no_tailwind_tags"


def score_c6(growth: str, source: str) -> tuple[int, str]:
    g = (growth or "").lower()
    s = (source or "").lower()
    if any(
        x in g
        for x in (
            "ipo",
            "expansion",
            "facility",
            "hiring",
            "recruitment",
            "iso",
            "certification",
            "capex",
            "capacity",
            "filed",
            "listed",
        )
    ):
        return 1, "growth_or_ipo_signal_in_text"
    if "bse drhp" in s or "drhp" in g:
        return 1, "ipo_drhp_source_signal"
    return 0, "weak_growth_signal"


def read_csv(path: Path, source_label: str) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_source_file"] = source_label
    return rows


def normalize_row(r: dict[str, str], file_tag: str) -> dict[str, str]:
    """Map heterogeneous columns to canonical keys."""
    cn = (
        r.get("Company Name")
        or r.get("Company name")
        or r.get("company_name")
        or ""
    ).strip()
    city = (
        r.get("City/Location")
        or r.get("City / Location")
        or r.get("City")
        or ""
    ).strip()
    ind = (
        r.get("Industry Segment")
        or r.get("Industry segment")
        or ""
    ).strip()
    web = (r.get("Website") or "").strip()
    rev = (r.get("Revenue Band") or "").strip()
    dsir = (r.get("DSIR Recognition Evidence") or "").strip()
    prom = (
        r.get("Promoter/Decision-Maker")
        or r.get("Promoter / Decision-Maker")
        or ""
    ).strip()
    growth = (r.get("Growth Signals") or "").strip()
    src = (r.get("Source") or r.get("_source_file") or file_tag).strip()

    blob = " ".join([cn, city, ind, web, rev, dsir, prom, growth])
    cin = extract_cin(blob)

    return {
        "company_name": cn,
        "city_raw": city,
        "industry_raw": ind,
        "website": web,
        "revenue_raw": rev,
        "dsir": dsir,
        "promoter": prom,
        "growth": growth,
        "source": src,
        "cin": cin or "",
        "norm_key": normalize_company_key(cn),
        "_blob": blob,
        "_source_file": r.get("_source_file", file_tag),
    }


def fuzzy_dedupe_groups(records: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Partition records into duplicate groups using CIN then fuzzy name."""
    by_cin: dict[str, list[dict[str, str]]] = defaultdict(list)
    no_cin: list[dict[str, str]] = []
    for rec in records:
        if rec["cin"]:
            by_cin[rec["cin"]].append(rec)
        else:
            no_cin.append(rec)

    groups: list[list[dict[str, str]]] = list(by_cin.values())

    # Exact norm_key buckets
    by_norm: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in no_cin:
        k = rec["norm_key"]
        if len(k) >= 6:
            by_norm[k].append(rec)
        else:
            groups.append([rec])

    for bucket in by_norm.values():
        if len(bucket) == 1:
            groups.append(bucket)
            continue
        # Further fuzzy merge within bucket (handles typos)
        used = set()
        for i, a in enumerate(bucket):
            if i in used:
                continue
            cl = [a]
            used.add(i)
            for j, b in enumerate(bucket):
                if j in used:
                    continue
                ra = SequenceMatcher(None, a["norm_key"], b["norm_key"]).ratio()
                if ra >= 0.98 or (
                    min(len(a["norm_key"]), len(b["norm_key"])) >= 8
                    and ra >= 0.92
                    and a["company_name"][:12].upper() == b["company_name"][:12].upper()
                ):
                    cl.append(b)
                    used.add(j)
            groups.append(cl)

    return groups


def merge_group(rows: list[dict[str, str]]) -> dict[str, str]:
    """Merge duplicate rows; prefer higher-quality fields."""
    priority = {"drhp_companies_specified_format.csv": 3, "final_merged_dsir_promoters.csv": 2, "data_with_dsir_recognition.csv": 1}

    def sort_key(r: dict[str, str]) -> int:
        return priority.get(r["_source_file"], 0)

    rows = sorted(rows, key=sort_key, reverse=True)

    company = merge_text_best(*(r["company_name"] for r in rows))
    city = pick_city(*(r["city_raw"] for r in rows))
    industry_raw = merge_text_best(*(r["industry_raw"] for r in rows), min_len=8)
    industry_norm = normalize_industry_segment(industry_raw)
    website = merge_text_best(*(r["website"] for r in rows))
    revenue = normalize_revenue_band(
        merge_text_best(*(r["revenue_raw"] for r in rows), min_len=3)
    )
    dsir = merge_text_best(*(r["dsir"] for r in rows), min_len=20)
    promoter = merge_text_best(*(r["promoter"] for r in rows), min_len=15)
    growth = merge_text_best(*(r["growth"] for r in rows), min_len=10)

    sources = sorted({r["_source_file"] for r in rows})
    source_out = "+".join(sources)

    diff_ev = build_differentiation_evidence(dsir, growth, industry_raw)
    tailwinds = infer_tailwinds(industry_norm, growth, industry_raw, source_out)

    c1, n1 = score_c1(industry_norm, industry_raw)
    c2, n2 = score_c2(city)
    c3, n3 = score_c3(diff_ev, dsir)
    c4, n4 = score_c4(promoter)
    c5, n5 = score_c5(tailwinds)
    c6, n6 = score_c6(growth, source_out)

    met_count = c1 + c2 + c3 + c4 + c5 + c6
    combined = round(100 * met_count / 6)
    weighted = round(
        100
        * (
            ICP_WEIGHTS["c1"] * c1
            + ICP_WEIGHTS["c2"] * c2
            + ICP_WEIGHTS["c3"] * c3
            + ICP_WEIGHTS["c4"] * c4
            + ICP_WEIGHTS["c5"] * c5
            + ICP_WEIGHTS["c6"] * c6
        ),
        1,
    )

    cin_final = ""
    for r in rows:
        if r["cin"]:
            cin_final = r["cin"]
            break
    if not cin_final:
        cin_final = extract_cin(" ".join(r["_blob"] for r in rows)) or ""

    return {
        "Company Name": company,
        "CIN": cin_final,
        "City": city,
        "Industry Segment": industry_norm,
        "Industry Segment (raw)": industry_raw[:200],
        "Website": website,
        "Revenue Band": revenue,
        "Differentiation Evidence": diff_ev,
        "Promoter/Decision-Maker": promoter,
        "Growth Signals": growth,
        "Tailwinds": tailwinds,
        "Source": source_out,
        "Evidence Tags Normalized": slug_tags(f"{diff_ev}|{tailwinds}|{growth}"),
        "C1_Manufacturer": str(c1),
        "C1_Notes": n1,
        "C2_India_Based": str(c2),
        "C2_Notes": n2,
        "C3_Differentiation": str(c3),
        "C3_Notes": n3,
        "C4_Technical_DM": str(c4),
        "C4_Notes": n4,
        "C5_Tailwinds": str(c5),
        "C5_Notes": n5,
        "C6_Growth_Signals": str(c6),
        "C6_Notes": n6,
        "ICP_Criteria_Met_Count": str(met_count),
        "ICP_Coverage_Score": str(combined),
        "ICP_Weighted_Score": str(weighted),
        "_dedupe_group_size": str(len(rows)),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build unified ICP master + ranked shortlist.")
    p.add_argument(
        "--top-n",
        type=int,
        default=1000,
        metavar="N",
        help="Write top N ranked rows to icp_shortlist_top{N}.csv (default: 1000).",
    )
    p.add_argument(
        "--no-shortlist",
        action="store_true",
        help="Skip writing the shortlist file.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raw: list[dict[str, str]] = []
    raw.extend(read_csv(F_DRHP, "drhp_companies_specified_format.csv"))
    raw.extend(read_csv(F_MERGED, "final_merged_dsir_promoters.csv"))
    raw.extend(read_csv(F_DATA, "data_with_dsir_recognition.csv"))

    canon = [normalize_row(r, r["_source_file"]) for r in raw]
    groups = fuzzy_dedupe_groups(canon)
    merged = [merge_group(g) for g in groups]

    fieldnames = [
        "Company Name",
        "City",
        "Industry Segment",
        "Website",
        "Revenue Band",
        "Differentiation Evidence",
        "Promoter/Decision-Maker",
        "Growth Signals",
        "Tailwinds",
        "Source",
        "CIN",
        "Industry Segment (raw)",
        "Evidence Tags Normalized",
        "C1_Manufacturer",
        "C1_Notes",
        "C2_India_Based",
        "C2_Notes",
        "C3_Differentiation",
        "C3_Notes",
        "C4_Technical_DM",
        "C4_Notes",
        "C5_Tailwinds",
        "C5_Notes",
        "C6_Growth_Signals",
        "C6_Notes",
        "ICP_Criteria_Met_Count",
        "ICP_Coverage_Score",
        "ICP_Weighted_Score",
        "_dedupe_group_size",
    ]

    merged.sort(
        key=lambda x: (
            -float(x["ICP_Weighted_Score"]),
            -int(x["ICP_Criteria_Met_Count"]),
            x["Company Name"].upper(),
        )
    )

    with OUT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)

    print(f"Wrote {OUT.name}: {len(merged)} unified rows from {len(raw)} raw rows.")

    if not args.no_shortlist and args.top_n > 0:
        short_path = ROOT / f"icp_shortlist_top{args.top_n}.csv"
        top = merged[: min(args.top_n, len(merged))]
        with short_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(top)
        print(f"Wrote {short_path.name}: {len(top)} rows (top-{args.top_n} cap).")


if __name__ == "__main__":
    main()
