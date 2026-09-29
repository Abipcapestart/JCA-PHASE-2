"""Run the REAL Phase 1 pipeline for one drug, saving every intermediate
step's output to a numbered JSON file, so a wrong final result can be traced
back to the exact step that produced it. Does not modify any phase1_scoping
file - this script only imports and calls its existing functions in the same
order orchestrator.py's run() does, adding a save-to-disk after each step.

Usage:
    python scripts/run_phase1_traced.py --drug lurbinectedin
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_THIS_DIR)
_PLAN_DIR = os.path.dirname(_PACKAGE_DIR)
_PHASE1_SCOPING_DIR = os.path.join(_PLAN_DIR, "phase1_scoping")
for _p in (_PACKAGE_DIR, _PHASE1_SCOPING_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scoping_config as config  # noqa: E402
from context.therapeutic_area import resolve_therapeutic_areas  # noqa: E402
from consolidation.consolidate import (  # noqa: E402
    build_by_member_state, build_consolidated_comparators, build_consolidated_outcomes,
    compose_rationale, compose_outcome_rationale,
)
from harmonization.harmonize import (  # noqa: E402
    canonicalize_finding_populations, llm_harmonize_comparators, llm_harmonize_outcomes,
)
from input.structuring_agent import structure_pi  # noqa: E402
from input.validation_agent import validate_pi  # noqa: E402
from pico_set_derivation import derive_pico_sets  # noqa: E402
from schema import FinalScopingOutput  # noqa: E402
from source_reverification import (  # noqa: E402
    merge_duplicate_comparators, reverify_combination_comparators, validate_population_relevance,
)

DRUG_INPUTS = {
    "lurbinectedin": {
        "population_fields": {
            "indication_disease": (
                "Extensive-stage small cell lung cancer (ES-SCLC) whose disease has not "
                "progressed after first-line induction therapy"),
        },
        "intervention_fields": {"product_name": "Lurbinectedin"},
        "raw_population_text": (
            "Extensive-stage small cell lung cancer (ES-SCLC) whose disease has not progressed "
            "after first-line induction therapy.\n"
            "Age group: Adults.\n"
            "Stage/severity: Extensive-stage disease; maintenance setting - disease not progressed "
            "after 1L induction.\n"
            "Molecular/biomarker status: All-comers - no biomarker selection required.\n"
            "Prior therapy/line: Completed first-line induction with atezolizumab + carboplatin + "
            "etoposide, without progression.\n"
            "Given as maintenance therapy in combination with atezolizumab.\n"
            "Disease subtype/histology: Small cell lung cancer (SCLC), extensive-stage.\n"
            "Orphan/rare-disease designation: Yes (EU/3/19/2143)."
        ),
        "raw_intervention_text": (
            "Product name (INN): Lurbinectedin, in combination with atezolizumab.\n"
            "Therapeutic class/mechanism: Selective inhibitor of oncogenic transcription (binds "
            "CG-rich DNA promoter sequences, evicts oncogenic transcription factors, stalls RNA "
            "polymerase II, leading to cell cycle arrest and apoptosis; also modulates "
            "tumour-associated macrophages).\n"
            "Route of administration: Intravenous infusion.\n"
            "Dose: 3.2 mg/m^2 by IV infusion over 60 minutes.\n"
            "Treatment duration: Repeated every 21 days until disease progression or unacceptable "
            "toxicity.\n"
            "Line of therapy: First-line maintenance (following 1L induction).\n"
            "Dosing schedule: Every 21 days (Q3W), continuous.\n"
            "Claimed/anticipated indication wording: Lurbinectedin, in combination with "
            "atezolizumab, is indicated for the maintenance treatment of adult patients with "
            "ES-SCLC whose disease has not progressed after first-line induction therapy with "
            "atezolizumab, carboplatin and etoposide.\n"
            "Product modality: Small molecule (alkaloid-derived DNA-binding agent).\n"
            "Regulatory pathway: Standard marketing authorisation (not conditional); orphan "
            "medicinal product.\n"
            "Pharmaceutical form/strength: Powder for concentrate for solution for infusion.\n"
            "Pivotal trial identifier(s): IMforte, NCT05091567."
        ),
    },
}


def _asdict(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


def _save(out_dir: str, step_no: str, name: str, payload) -> str:
    path = os.path.join(out_dir, f"{step_no}_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"[saved] {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real Phase 1 pipeline with per-step tracing.")
    parser.add_argument("--drug", required=True, choices=sorted(DRUG_INPUTS.keys()))
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    drug_input = DRUG_INPUTS[args.drug]
    out_dir = args.out_dir or os.path.join(_PACKAGE_DIR, "results", "phase1_trace", args.drug)
    os.makedirs(out_dir, exist_ok=True)

    t_start = time.time()

    # ---- Step 1: P&I Validation --------------------------------------------
    print("=== Step 1: P&I Validation ===")
    validation = validate_pi(drug_input["population_fields"], drug_input["intervention_fields"])
    _save(out_dir, "01", "validation", validation.to_dict())
    if not validation.passed:
        print("BLOCKED at P&I Validation - stopping.")
        return

    # ---- Step 2: Input Structuring ------------------------------------------
    print("=== Step 2: Input Structuring ===")
    structured = structure_pi(drug_input["raw_population_text"], drug_input["raw_intervention_text"])
    _save(out_dir, "02", "structured", structured.to_dict())

    outputs = []
    for pop_idx, population in enumerate(structured.populations):
        pop_tag = f"pop{pop_idx}"
        indication = (population.fields.get("indication_disease").value
                      if population.fields.get("indication_disease") else "") or ""
        intervention_name = (structured.intervention.get("product_name").value
                              if structured.intervention.get("product_name") else "") or ""
        subtype = (population.fields.get("disease_subtype_histology").value
                   if population.fields.get("disease_subtype_histology") else "") or ""
        icd = (population.fields.get("icd_disease_code").value
               if population.fields.get("icd_disease_code") else "") or ""
        orphan_field = population.fields.get("orphan_rare_disease_flag")
        orphan = bool(orphan_field and str(orphan_field.value or "").strip().lower() in ("yes", "true"))

        print(f"=== Step 3 [{pop_tag}]: Therapeutic Area Resolution ===")
        areas = resolve_therapeutic_areas(indication, disease_subtype=subtype, icd_code=icd,
                                           orphan_rare_disease_flag=orphan)
        _save(out_dir, "03", f"therapeutic_areas_{pop_tag}", _asdict(areas))

        print(f"=== Step 4 [{pop_tag}]: Retrieval Orchestrator (27-state loop) - this takes several minutes ===")
        from retrieval_orchestrator import run_full_27_state_retrieval
        t0 = time.time()
        retrieval_result = run_full_27_state_retrieval(indication, intervention_name)
        findings, ledger = retrieval_result["findings"], retrieval_result["ledger"]
        print(f"    retrieval took {time.time() - t0:.1f}s, {len(findings)} raw findings")
        _save(out_dir, "04", f"retrieval_findings_{pop_tag}", [_asdict(f) for f in findings])
        _save(out_dir, "04", f"retrieval_ledger_summary_{pop_tag}", {
            "summary": ledger.summary(),
            "identified_states": ledger.identified_states(),
            "not_identified_states": ledger.not_identified_states(),
            "entries": [_asdict(e) for e in ledger.all_entries()],
        })
        _save(out_dir, "04", f"retrieval_population_scope_exclusions_{pop_tag}",
              [_asdict(x) if dataclasses.is_dataclass(x) else x
               for x in retrieval_result.get("population_scope_exclusions", [])])

        # ---- Step 5: Harmonization -------------------------------------------
        print(f"=== Step 5 [{pop_tag}]: Harmonization ===")
        findings = canonicalize_finding_populations(findings, indication)
        _save(out_dir, "05", f"canonicalized_findings_{pop_tag}", [_asdict(f) for f in findings])
        comparator_groups = llm_harmonize_comparators(findings)
        _save(out_dir, "05", f"comparator_groups_{pop_tag}", [_asdict(g) for g in comparator_groups])
        outcome_groups = llm_harmonize_outcomes(findings)
        _save(out_dir, "05", f"outcome_groups_{pop_tag}", [_asdict(g) for g in outcome_groups])

        # ---- Step 6: Consolidation ---------------------------------------------
        print(f"=== Step 6 [{pop_tag}]: Consolidation ===")
        comparators = build_consolidated_comparators(comparator_groups, rationale_fn=compose_rationale)
        _save(out_dir, "06", f"consolidated_comparators_pre_reverify_{pop_tag}", [_asdict(c) for c in comparators])

        comparators, reverify_log = reverify_combination_comparators(
            comparators, requested_intervention=intervention_name)
        _save(out_dir, "06", f"reverify_log_{pop_tag}", reverify_log)
        _save(out_dir, "06", f"consolidated_comparators_post_reverify_{pop_tag}", [_asdict(c) for c in comparators])

        comparators, merge_log = merge_duplicate_comparators(comparators)
        _save(out_dir, "06", f"merge_log_{pop_tag}", merge_log)

        comparators, relevance_log = validate_population_relevance(comparators, indication)
        _save(out_dir, "06", f"population_relevance_log_{pop_tag}", relevance_log)
        _save(out_dir, "06", f"consolidated_comparators_final_{pop_tag}", [_asdict(c) for c in comparators])

        outcomes = build_consolidated_outcomes(outcome_groups, rationale_fn=compose_outcome_rationale)
        _save(out_dir, "06", f"consolidated_outcomes_{pop_tag}", _asdict(outcomes))

        by_state, not_identified, summary = build_by_member_state(comparators, ledger)
        _save(out_dir, "06", f"by_member_state_{pop_tag}", {
            "by_member_state": [_asdict(e) for e in by_state],
            "not_identified_states": not_identified,
            "summary": _asdict(summary),
        })

        # ---- Step 7: Stage-2 PICO-set derivation (deterministic) ------------
        print(f"=== Step 7 [{pop_tag}]: PICO Set Derivation ===")
        pico_sets = derive_pico_sets(population.population_label or "licensed", intervention_name, comparators)
        _save(out_dir, "07", f"pico_sets_{pop_tag}", [p.to_dict() for p in pico_sets])

        output = FinalScopingOutput(
            requested_population=indication,
            requested_intervention=intervention_name,
            population_subset_label=population.population_label,
            comparators=comparators,
            not_identified_states=not_identified,
            by_member_state=by_state,
            member_state_summary=summary,
            outcomes=outcomes,
            pico_sets=[p.to_dict() for p in pico_sets],
        )
        _save(out_dir, "08", f"final_scoping_output_{pop_tag}", output.to_dict())
        outputs.append(output)

    orchestrator_result = {
        "status": "OK",
        "validation": validation.to_dict(),
        "structured": structured.to_dict(),
        "outputs": [o.to_dict() for o in outputs],
    }
    _save(out_dir, "09", "orchestrator_result", orchestrator_result)

    print(f"\nTotal elapsed: {time.time() - t_start:.1f}s")
    print(f"Full traced output directory: {out_dir}")


if __name__ == "__main__":
    main()
