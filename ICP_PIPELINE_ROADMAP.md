# ICP pipeline roadmap (Steps 1–9)

Reviewer-oriented flow: each stage is labeled **Step *n*** with the main **risk** called out. Optional **LLM blend** sits after Step 7 when API access is available.

```mermaid
flowchart TB
  subgraph Step1["Step 1 — Base SME register — Risk: stale or incomplete master data"]
    DATA[data.csv]
  end

  subgraph Step2["Step 2 — DSIR reference corpus — Risk: directory version drift vs applicant reality"]
    DSIRTXT[dsir_directory_*.txt]
  end

  subgraph Step3["Step 3 — BSE SME DRHP listing — Risk: scrape drift, broken or changed URLs"]
    DRHP_ROWS[drhp_rows_with_links.csv]
    DRHP_NAMES[drhp_company_names.txt]
  end

  subgraph Step4["Step 4 — DRHP PDF/ZIP enrichment — Risk: download failures, extraction gaps, heuristic column noise"]
    ENRICH_PY[enrich_drhp_columns.py]
    DRHP_FMT[drhp_companies_specified_format.csv]
    DRHP_ROWS --> ENRICH_PY
    ENRICH_PY --> DRHP_FMT
  end

  subgraph Step5["Step 5 — DSIR evidence + promoter merge — Risk: directory-match FP/FN; promoter parsing errors"]
    BUILD_PY[build_final_dsir_promoter_csv.py]
    DATA_DSIR[data_with_dsir_recognition.csv]
    FINAL_MERGED[final_merged_dsir_promoters.csv]
    DATA --> BUILD_PY
    DSIRTXT --> BUILD_PY
    DRHP_FMT --> BUILD_PY
    BUILD_PY --> DATA_DSIR
    BUILD_PY --> FINAL_MERGED
  end

  subgraph Step6["Step 6 — Unified ICP merge — Risk: dedupe errors when CIN missing; fuzzy clustering mistakes"]
    MERGE_PY[merge_unified_icp.py]
    MASTER[unified_icp_master.csv]
    DATA_DSIR --> MERGE_PY
    FINAL_MERGED --> MERGE_PY
    DRHP_FMT --> MERGE_PY
    MERGE_PY --> MASTER
  end

  subgraph Step7["Step 7 — Weighted scoring + sort + shortlist — Risk: weighting vs business intent; rank sensitivity"]
    SHORT[icp_shortlist_topN.csv]
    MERGE_PY --> SHORT
  end

  subgraph OptionalLLM["Optional — LLM blend after Step 7 — Risk: score variance; alpha blending masks weak rule signals"]
    SCORE_PY[score_icp_llm.py]
    LLM_SCORED[icp_shortlist_topN_llm_scored.csv]
    CKPT[checkpoint .jsonl]
    SHORT --> SCORE_PY
    SCORE_PY --> LLM_SCORED
    SCORE_PY --> CKPT
  end

  subgraph Step8["Step 8 — Hard filters + dedupe — Risk: false negatives from aggressive URL/revenue/name rules"]
    CLEAN8[clean_icp_step8.py]
    S8_OK[icp_shortlist_step8_clean.csv]
    S8_REJ[icp_step8_rejects.csv]
    LLM_SCORED --> CLEAN8
    SHORT --> CLEAN8
    CLEAN8 --> S8_OK
    CLEAN8 --> S8_REJ
  end

  subgraph Step9["Step 9 — QA + calibration queue — Risk: too many flagged; calibration skewing reviewer workload"]
    QA_PY[qa_icp_step9.py]
    S9_FLAG[icp_step9_flagged.csv]
    S9_OK[icp_step9_clean.csv]
    S8_OK --> QA_PY
    QA_PY --> S9_FLAG
    QA_PY --> S9_OK
  end

  subgraph Deliverables["Deliverables — primary reviewer handoff files"]
    D_MASTER[unified_icp_master.csv]
    D_SHORT[icp_shortlist_topN.csv]
    D_S8[icp_shortlist_step8_clean.csv]
    D_S9[icp_step9_clean.csv]
  end

  MASTER --> D_MASTER
  SHORT --> D_SHORT
  S8_OK --> D_S8
  S9_OK --> D_S9
```

## Deliverables (summary)

| File | Produced after |
|------|----------------|
| `unified_icp_master.csv` | Step 6 (same script run as Step 7; single logical artefact for the merged schema) |
| `icp_shortlist_topN.csv` | Step 7 (`merge_unified_icp.py`, `--top-n`; e.g. `icp_shortlist_top1000.csv`) |
| `icp_shortlist_step8_clean.csv` | Step 8 |
| `icp_step9_clean.csv` | Step 9 |

Supporting artefacts not listed above include rejects (`icp_step8_rejects.csv`), QA-flagged rows (`icp_step9_flagged.csv`), summaries (`icp_step8_summary.txt`, `icp_step9_summary.txt`), and optional LLM checkpoint/scored CSVs.

## Notes

- **Steps 6–7** are implemented together in `merge_unified_icp.py`; the diagram separates them so reviewers map roadmap steps to scoring behaviour (dedupe/normalized schema vs weighted rank + shortlist).
- **`--rules-only`** on `score_icp_llm.py` skips LLM calls; Step 8 can consume the Step 7 shortlist directly.
