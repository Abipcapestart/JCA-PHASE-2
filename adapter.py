"""Phase 1 (jca_phase1) -> Phase 2 contract adapter.

Pure field selection/slicing over jca_phase1's ACTUAL, real output - no new
Phase 1 fields are required. This module never invents `user_added`
(manually-added) comparator/outcome records - `origin` is read straight
through from Phase 1's own `ConsolidatedComparator.origin` /
`ConsolidatedOutcome.origin`.

Input: the exact dict shape `jca_phase1.orchestrator.run_phase1()` returns,
i.e. `Phase1Output.to_dict()` (see jca_phase1/jca_phase1/schema.py). A single
run's `output.populations` list may itself carry MORE THAN ONE population
struct - e.g. `populations[0]` (licensed) and `populations[1]`
(intended_to_treat) - per jca_phase1/ARCHITECTURE.md ("licensed population
(+ ITT population if declared)"). `comparators`, `outcomes`,
`not_identified_states` and `member_state_summary` all live at the TOP LEVEL
of `Phase1Output`, shared across every population struct in that run (Phase 1
already scope-adjudicates them across all declared populations - see each
`scope_boundaries[].out_of_bounds_rule`, e.g. `"flag_as_itt"` - rather than
partitioning them per population), so every `PopulationInstance` built from
one run reuses that same top-level evidence.

So `adapt_orchestrator_result()` takes a LIST of one-or-more such dicts - one
per Phase 1 run - and produces one `PopulationInstance` PER POPULATION STRUCT
across all of them (a single run with 2 population structs yields 2
`PopulationInstance`s; a legacy caller that instead persists one run per
population, e.g. a separate licensed run and a separate intended-to-treat
run for the same product, still works the same way - one `PopulationInstance`
per run in that case). A single dict is also accepted and treated as a list
of one, for the common single-run case.

`Phase1Output` has no `status` field (a BLOCKED P&I-validation run raises
`BlockedError` before any `Phase1Output` is constructed, so a saved run JSON
is implicitly a completed one) - the soft guard here instead checks
`input_validation.overall_status`, in case a caller persisted a run that
bypassed that exception.

Adapts defensively: a single malformed per-Member-State entry (missing/blank
`comparator_scenario`, unknown/non-canonical state) is skipped with a
recorded warning rather than crashing the whole adapter run - Phase 1 data
may be genuinely incomplete, and Phase 2 must fail safely for the *specific*
affected item, not the entire request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, Union

from pydantic import ValidationError

from schemas import (
    ComparatorInput, EvidenceItem, InterventionInput, MemberStateComparatorRequirement,
    OutcomeGroupLabel, OutcomeInput, Phase2Input, PopulationInstance,
)

# Phase 1's 4 outcome buckets -> the SME Phase 2 prompt's 3 named groups.
# `clinician_patient_reported` folds into quality_of_life_and_symptoms per
# the recommended engineering default - pending SME confirmation, NOT an SME
# requirement.
_OUTCOME_BUCKET_TO_GROUP = {
    "clinical_effectiveness": OutcomeGroupLabel.EFFICACY,
    "safety": OutcomeGroupLabel.SAFETY,
    "quality_of_life": OutcomeGroupLabel.QUALITY_OF_LIFE_AND_SYMPTOMS,
    "clinician_patient_reported": OutcomeGroupLabel.QUALITY_OF_LIFE_AND_SYMPTOMS,
}

# jca_phase1's per-Member-State verdict that means "this comparator IS
# standard of care in this state, per a state-attributed source" - the only
# verdict that represents a genuine per-state requirement. `not_established`
# (searched, nothing found) and `not_used` are not requirements and must
# never become a per-state row (see jca_phase1/agents/a09_a13_validation.py
# assign_member_states()).
_STATE_STANDARD_OF_CARE = "standard_of_care"


@dataclass
class AdaptationResult:
    phase2_input: Phase2Input
    warnings: List[str] = field(default_factory=list)


class Phase1OutputAdapterError(Exception):
    """Raised only for structural failures that make adaptation impossible
    entirely (e.g. a BLOCKED P&I validation, missing intervention product
    name) - never for a single bad Member-State row, which is a warning
    instead."""


def unwrap_phase1_run(run: Dict[str, Any], run_index: int = 0) -> Dict[str, Any]:
    """Some callers persist jca_phase1 runs wrapped in an evaluation/harness
    envelope rather than a bare `Phase1Output.to_dict()` - e.g.
    `scripts/run_gt_eval.py` saves `{"drug", "status", "output": <Phase1Output
    .to_dict()>, "num_comparators", "bedrock", "tavily", ...}` on success, but
    `{"drug", "status": "blocked"|"failed", "input_validation"|"error", ...}`
    with NO `output` key at all when the run didn't complete (see
    scripts/gt_eval_output/*.json and scripts/run_gt_eval.py's `run_one()`).
    A bare `Phase1Output.to_dict()` never has a `status` key, so that alone
    (plus the absence of `comparators`, which a bare output always has) is
    enough to detect the envelope reliably in every one of the harness's
    three outcomes. `status` there is the harness's own complete/blocked/
    failed tri-state, distinct from (and checked before) `input_validation.
    overall_status` inside `output`."""
    if "status" in run and "comparators" not in run:
        status = run.get("status")
        if status != "complete":
            detail = run.get("error") or (run.get("input_validation") or {}).get("overall_status") or status
            raise Phase1OutputAdapterError(
                f"Phase 1 run #{run_index + 1} ({run.get('drug', '?')!r}) did not complete "
                f"(status={status!r}): {detail}")
        return run.get("output") or {}
    return run


def adapt_orchestrator_result(
    phase1_outputs: Union[Dict[str, Any], List[Dict[str, Any]]],
    run_id: str, source_ref: str = "",
) -> AdaptationResult:
    if isinstance(phase1_outputs, dict):
        phase1_outputs = [phase1_outputs]
    if not phase1_outputs:
        raise Phase1OutputAdapterError("No Phase 1 output provided - at least one run is required.")

    warnings: List[str] = []
    intervention = None
    populations: List[PopulationInstance] = []

    for i, run in enumerate(phase1_outputs):
        run = unwrap_phase1_run(run, run_index=i)
        overall_status = (run.get("input_validation") or {}).get("overall_status")
        if overall_status and overall_status != "PASS":
            raise Phase1OutputAdapterError(
                f"Phase 1 run #{i + 1} did not pass P&I validation (overall_status="
                f"{overall_status!r}) - Phase 2 cannot proceed on a BLOCKED Phase 1 result.")

        run_intervention = _adapt_intervention(run.get("intervention") or {})
        if not run_intervention.product_name:
            raise Phase1OutputAdapterError(
                f"Phase 1 run #{i + 1} has no intervention product_name_inn - mandatory field missing.")
        if intervention is None:
            intervention = run_intervention
        elif run_intervention.product_name != intervention.product_name:
            warnings.append(
                f"Phase 1 run #{i + 1} intervention product_name_inn "
                f"({run_intervention.product_name!r}) differs from run #1's "
                f"({intervention.product_name!r}) - using run #1's intervention for all populations.")

        run_pop_structs = run.get("populations") or []
        if not run_pop_structs:
            warnings.append(f"Phase 1 run #{i + 1} has no populations - skipped.")
            continue
        for pop_struct in run_pop_structs:
            pop_instance, pop_warnings = _adapt_population(run, pop_struct)
            populations.append(pop_instance)
            warnings.extend(pop_warnings)

    try:
        phase2_input = Phase2Input(
            run_id=run_id, source_phase1_run_ref=source_ref,
            intervention=intervention, populations=populations,
        )
    except ValidationError as exc:
        raise Phase1OutputAdapterError(f"Adapted Phase 2 input failed schema validation: {exc}") from exc

    return AdaptationResult(phase2_input=phase2_input, warnings=warnings)


def _adapt_intervention(intervention_struct: Dict[str, Any]) -> InterventionInput:
    fields = intervention_struct.get("fields") or {}
    product_name = _field_value(fields, "product_name_inn")
    attributes = {
        k: _field_value(fields, k) for k in fields
        if k != "product_name_inn" and _field_value(fields, k)
    }
    for cf in intervention_struct.get("custom_fields") or []:
        label, value = (cf or {}).get("label", ""), (cf or {}).get("value", "")
        if label and value:
            attributes[label] = str(value).strip()
    return InterventionInput(product_name=product_name or "", attributes=attributes)


def _adapt_population(run: Dict[str, Any], pop_struct: Dict[str, Any]) -> Tuple[PopulationInstance, List[str]]:
    """Adapts one population struct from `run["populations"]`. `comparators`
    /`outcomes`/`not_identified_states`/`member_state_summary` are read from
    the run's TOP LEVEL (shared/pre-scope-adjudicated across every population
    in this run - see module docstring), not from `pop_struct` itself."""
    warnings: List[str] = []
    fields = pop_struct.get("fields") or {}
    indication = _field_value(fields, "indication_disease") or ""
    attributes = {
        k: _field_value(fields, k) for k in fields
        if k != "indication_disease" and _field_value(fields, k)
    }
    for cf in pop_struct.get("custom_fields") or []:
        label, value = (cf or {}).get("label", ""), (cf or {}).get("value", "")
        if label and value:
            attributes[label] = str(value).strip()

    comparators: List[ComparatorInput] = []
    for raw_comparator in run.get("comparators", []) or []:
        adapted, comp_warnings = _adapt_comparator(raw_comparator)
        if adapted is not None:
            comparators.append(adapted)
        warnings.extend(comp_warnings)

    outcomes = _adapt_outcomes(run.get("outcomes") or {})

    instance = PopulationInstance(
        population_label=pop_struct.get("population_id") or "licensed",
        population_subset_label="",
        indication_disease=indication,
        attributes=attributes,
        comparators=comparators,
        outcomes=outcomes,
        not_identified_states=list(run.get("not_identified_states") or []),
        member_state_summary=dict(run.get("member_state_summary") or {}),
    )
    return instance, warnings


def _source_url_map(raw_sources: List[Dict[str, Any]]) -> Dict[str, str]:
    """jca_phase1's `sources`/`evidence` are keyed by `source_id`; Phase 2's
    contract carries a plain source string per entry. Build the
    source_id -> url lookup once per comparator/outcome."""
    return {s.get("source_id", ""): (s.get("url") or s.get("source_id") or "")
            for s in raw_sources or [] if s.get("source_id")}


def _adapt_evidence(raw_evidence: List[Dict[str, Any]], url_by_source_id: Dict[str, str]) -> List[EvidenceItem]:
    return [
        EvidenceItem(
            source=url_by_source_id.get(e.get("source_id", ""), e.get("source_id", "")),
            quote=e.get("quote", ""),
        )
        for e in raw_evidence or []
    ]


def _adapt_comparator(raw: Dict[str, Any]) -> Tuple[Any, List[str]]:
    warnings: List[str] = []
    generic_name = raw.get("generic_name", "")
    if not generic_name:
        return None, [f"Skipped a comparator entry with no generic_name: {raw!r}"]

    # jca_phase1 carries comparator_scenario/retain_all_status once per
    # comparator (not per Member State, unlike the retired phase1_scoping
    # shape) - broadcast down to every retained per-state row so Agent 2's
    # per-state prompt payload (agents/agent2_pico_consolidation.py) is
    # unchanged.
    comparator_scenario = raw.get("comparator_scenario") or ""
    retain_all_status = raw.get("retain_all_status") or "unconfirmed"

    per_member_state: List[MemberStateComparatorRequirement] = []
    for raw_ms in raw.get("per_member_state", []) or []:
        if raw_ms.get("verdict") != _STATE_STANDARD_OF_CARE:
            continue  # not_established / not_used - no genuine per-state requirement
        if not comparator_scenario:
            warnings.append(
                f"Skipped {raw_ms.get('member_state', '')!r} for comparator {generic_name!r}: "
                f"comparator has no established comparator_scenario.")
            continue
        try:
            per_member_state.append(MemberStateComparatorRequirement(
                member_state=raw_ms.get("member_state", ""),
                tier=raw_ms.get("tier") or 0,
                source_reference=raw_ms.get("source_url") or raw_ms.get("source_id", ""),
                evidence_quote=raw_ms.get("evidence_quote", ""),
                comparator_scenario=comparator_scenario,
                retain_all_status=retain_all_status,
            ))
        except ValidationError as exc:
            warnings.append(
                f"Skipped a per-Member-State entry for comparator {generic_name!r}: {exc.errors()[0].get('msg')} "
                f"(raw={raw_ms!r})")

    url_by_source_id = _source_url_map(raw.get("sources", []) or [])
    evidence = _adapt_evidence(raw.get("evidence", []) or [], url_by_source_id)

    comparator = ComparatorInput(
        generic_name=generic_name,
        brand_names=list(raw.get("brand_names", []) or []),
        member_states=list(raw.get("member_states", []) or []),
        per_member_state=per_member_state,
        class_or_mechanism=raw.get("class_or_mechanism", ""),
        line_of_therapy=raw.get("line_of_therapy", ""),
        indication=raw.get("indication", ""),
        tiers=list(raw.get("tiers", []) or []),
        rationale=raw.get("rationale", ""),
        sources=list(url_by_source_id.values()),
        evidence=evidence,
        source_type=raw.get("origin") or "agent_found",
    )
    return comparator, warnings


def _adapt_outcomes(outcomes_dict: Dict[str, Any]) -> List[OutcomeInput]:
    result: List[OutcomeInput] = []
    for bucket, group in _OUTCOME_BUCKET_TO_GROUP.items():
        for raw in outcomes_dict.get(bucket, []) or []:
            url_by_source_id = _source_url_map(raw.get("sources", []) or [])
            evidence = _adapt_evidence(raw.get("evidence", []) or [], url_by_source_id)
            result.append(OutcomeInput(
                concept=raw.get("concept", ""),
                group=group,
                tiers=list(raw.get("tiers", []) or []),
                rationale=raw.get("rationale", ""),
                sources=list(url_by_source_id.values()),
                evidence=evidence,
                listed=raw.get("listed", True),
                source_type=raw.get("origin") or "agent_found",
            ))
    return result


def _field_value(fields: Dict[str, Any], key: str) -> str:
    entry = fields.get(key)
    if not entry:
        return ""
    if isinstance(entry, dict):
        return (entry.get("value") or "").strip() if entry.get("value") else ""
    return str(entry).strip()
