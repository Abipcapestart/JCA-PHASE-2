# Real end-to-end trace: Phase 1 (Lurbinectedin) → Phase 2

Drug: **Lurbinectedin** (in combination with atezolizumab), from `MadeAI_JCA_GroundTruth_PICO Scoping.xlsx`.
All steps below are **real** — real Tavily searches, real Bedrock calls, no mocks. Total Phase 1 runtime: 733s (~12 min), 247 raw findings across all 27 states + Tier 2/3.

All intermediate artifacts are saved in this directory (`results/phase1_trace/lurbinectedin/`), numbered by pipeline step, plus the full console log in `results/phase1_trace/lurbinectedin_run.log`.

## Step-by-step trace

| Step | File(s) | Result | Verdict |
|---|---|---|---|
| 1. P&I Validation | `01_validation.json` | `overall_status: PASS`, no fix/check items | ✅ Correct |
| 2. Input Structuring | `02_structured.json` | Population/intervention fields correctly tagged CONFIRMED against the real free-text input | ✅ Correct |
| 3. Therapeutic Area Resolution | `03_therapeutic_areas_pop0.json` | `["Oncology", "Respiratory"]`, correctly flagged `needs_llm_adjudication: true` | ✅ Correct |
| 4. Retrieval (27-state + Tier 2/3) | `04_retrieval_findings_pop0.json` | 247 raw findings, real sources cited (guideline PDFs, HTA sites, trial registries) | ✅ Retrieval itself worked — **but see root cause below: `comparator_scenario` is absent from every single finding** |
| 5. Harmonization | `05_comparator_groups_pop0.json` | 9 comparator groups formed correctly by generic substance | ✅ Grouping logic correct |
| 6. Consolidation (population-relevance filtering) | `06_population_relevance_log_pop0.json`, `06_consolidated_comparators_final_pop0.json` | 7 of 9 groups correctly EXCLUDED (Chemotherapy, Carboplatin+Etoposide, Cisplatin+Etoposide, Placebo+Carboplatin+Etoposide, Topotecan, Irinotecan, CAV) — all confirmed by an independent LLM re-check to be induction-phase/relapsed-line comparators, not relevant to this drug's specific maintenance-phase population. Only "Atezolizumab" and "Atezolizumab without lurbinectedin" survive. | ✅ **Correct and clinically sound** — matches the Ground Truth's own framing (this JCA has exactly one narrow population; Atezolizumab-alone is the actual IMforte trial control arm) |
| 7. PICO Set Derivation (Phase 1's own, unrelated `pico_sets` field) | `07_pico_sets_pop0.json` | Produced, but — as already documented — not used by Phase 2 | N/A (correctly ignored by Phase 2) |
| 8. Final Scoping Output | `08_final_scoping_output_pop0.json` | 2 comparators, 25/27 states identified, `member_state_summary` sums correctly to 27 | ⚠️ Structurally valid, but see root cause |

## Root cause found: `comparator_scenario` is never populated anywhere in the real pipeline

Every single one of the 247 raw findings has **no `comparator_scenario` key at all** (confirmed directly in `04_retrieval_findings_pop0.json` — not `""`, not `null`, the key is simply absent). Traced this to source with a direct grep across the entire `phase1_scoping` codebase:

```
grep -rn "comparator_scenario\s*=" phase1_scoping/*.py phase1_scoping/**/*.py
  consolidation/consolidate.py:103:  comparator_scenario=mf.get("comparator_scenario", ""),
```

**That is the only real assignment in the entire codebase**, and it is a pass-through read (`.get(..., "")`) — Consolidation only forwards whatever value Harmonization's `member_findings` dict already carried. Harmonization, in turn, only forwards whatever the raw `RawFinding`/retrieval-stage dict already carried. Nothing anywhere in `retrieval_orchestrator.py` or `phase2/extraction/pico_extraction.py` ever **classifies** a finding into one of the 4 SME-defined scenarios (`unique` / `each_required` / `at_least_one` / `individualised`) in the first place — the one mention of `comparator_scenario` in `retrieval_orchestrator.py` is a docstring comment describing the *intended* schema, not code that sets it.

**Conclusion**: `schema.py` declares `comparator_scenario` as part of Phase 1's contract (and the field genuinely exists and is threaded correctly through every downstream step once set), but the classification logic that's supposed to populate it was never actually implemented anywhere in the retrieval/extraction pipeline. This is a **Phase 1 gap**, not a Phase 2 issue — and it directly contradicts the earlier architecture analysis's conclusion that "Phase 1 already provides `comparator_scenario` per Member State, no Phase 1 change needed." That conclusion was based on the field existing in `schema.py`'s type definitions (verified [P1-CODE]) and one stale saved example — it was never verified against a fresh, real run before now. **This real run is what surfaced that the assumption doesn't hold in practice.**

## What happened when this real output was fed into Phase 2

Ran via: `python run_pipeline.py --phase1-output results/phase1_trace/lurbinectedin/09_orchestrator_result.json --run-id e2e_lurbinectedin_real`

1. **Adapter**: correctly, defensively skipped every one of the ~100+ per-Member-State rows for "Atezolizumab" with a warning (`comparator_scenario` failed Pydantic's enum validation) — this is the adapter's designed fail-safe behavior working exactly as intended, not a new bug. Result: "Atezolizumab" survived as a comparator, but with `per_member_state = []` (no usable per-state data at all), even though its top-level `member_states` list still (correctly) named 25 states.
2. **Agent 2 (LLM)**: had no real per-state signal to consolidate from, and produced a `selection_basis: "unique"` selection with an **empty `member_states` list** (deterministic cross-state attachment had nothing to attach, since `per_member_state` was empty for every comparator).
3. **Agent 3**: wrote a rationale that (correctly) described the situation honestly: *"...though it is grounded in EU-wide general context rather than a confirmed listing from a specific Member State."*
4. **Agent 4**: **correctly BLOCKED the set** — both the deterministic 27-state completeness check (25 states unaccounted for) and the semantic groundedness/traceability checks fired, exactly as designed. Final `overall_status: BLOCKED`.

**This is the pipeline's safety net working as intended.** Phase 2 did not silently produce a plausible-looking but wrong PICO set from bad input — it correctly detected that the upstream data was too thin to consolidate safely and blocked, with a specific, actionable failure reason attached to the specific affected set.

## Recommendation

This is a genuine, actionable Phase 1 finding, separate from anything in the Phase 2 build: the retrieval/extraction pipeline needs an actual classification step (LLM or deterministic-with-LLM-fallback) that assigns each finding's `comparator_scenario` before Harmonization/Consolidation, matching what `schema.py` already declares as the contract. Until that exists, any drug whose comparators all come from this real pipeline will hit the same "empty per_member_state → blocked" outcome in Phase 2 — which is the correct, safe behavior given the current Phase 1 gap, but obviously not the goal state.
