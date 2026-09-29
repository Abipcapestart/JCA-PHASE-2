"""Agent 4 - Validation.

Deterministic checks (Member State completeness/duplication, tier
consistency against Phase 1's original records, constant-field consistency,
broadest-first ordering, rationale non-empty) run in pure Python - exactly
the kind of check the task explicitly forbids delegating to an LLM. Semantic
checks (rationale groundedness, methodology traceability) genuinely need
LLM judgment and run per-set with bounded concurrency. A set fails
("failed_blocked") if EITHER any deterministic check OR the semantic check
fails for it - never silently passed because one half looked fine.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from p2_config import EU_27_MEMBER_STATES, MAX_CONCURRENT_VALIDATION_CALLS, MAX_TOKENS_SEMANTIC_VALIDATION
from llm_client import LLMCallFailed, invoke_structured
from prompts.registry import get_registry
from schemas import (
    ComparatorInput, MemberStateCountCheck, Phase2PicoSet, PopulationInstance, RunLogEntry,
    SetAssemblyOutput, SetValidationResult, ValidationOutput,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SemanticCheckOutput(BaseModel):
    grounded: bool
    traceable: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def _check_member_state_completeness(assembly: SetAssemblyOutput) -> Optional[str]:
    """A Member State legitimately appearing in MULTIPLE distinct PICO sets
    for the same population is normal and expected - e.g. a state can have
    an at_least_one requirement against one comparator class AND a separate
    unique requirement for best supportive care, for the same population.
    This is confirmed directly by the SME prompt's OWN worked sample outputs
    (Agent 2/3's sample: Denmark appears in the Gefitinib, Afatinib, AND
    Best-supportive-care selections/sets simultaneously) - so "double-
    counted" does NOT mean "appears in more than one set". The only genuine
    contradiction this check can verify deterministically is a state
    appearing BOTH inside a PICO set's Member States AND in the
    not_identified_states list at once (claiming both "has a comparator"
    and "no comparator was identified" for the same population)."""
    all_set_states = {ms for s in assembly.pico_sets for ms in s.member_states}
    not_identified = set(assembly.not_identified_states)
    contradictions = all_set_states & not_identified
    all_accounted = all_set_states | not_identified
    missing = set(EU_27_MEMBER_STATES) - all_accounted
    unknown = all_accounted - set(EU_27_MEMBER_STATES)
    problems = []
    if contradictions:
        problems.append(
            f"Member State(s) appear both in a PICO set AND in not_identified_states "
            f"(contradiction): {sorted(contradictions)}")
    if missing:
        problems.append(f"Member State(s) missing from every set and not_identified_states: {sorted(missing)}")
    if unknown:
        problems.append(f"Unknown/non-canonical Member State(s) present: {sorted(unknown)}")
    return "; ".join(problems) if problems else None


def _check_constants(assembly: SetAssemblyOutput) -> Optional[str]:
    if not assembly.pico_sets:
        return None
    interventions = {s.intervention for s in assembly.pico_sets}
    outcomes_sets = {tuple(s.outcomes) for s in assembly.pico_sets}
    problems = []
    if len(interventions) > 1:
        problems.append(f"Intervention is not constant across sets for this population: {interventions}")
    if len(outcomes_sets) > 1:
        problems.append("Outcomes list is not identical across every set for this population")
    return "; ".join(problems) if problems else None


def _check_ordering(assembly: SetAssemblyOutput) -> Optional[str]:
    counts = [len(s.member_states) for s in assembly.pico_sets]
    if counts != sorted(counts, reverse=True):
        return "PICO sets are not ordered broadest-first by Member State count"
    return None


def _check_tier_consistency(assembly: SetAssemblyOutput, population: PopulationInstance) -> Dict[str, str]:
    comparator_by_name: Dict[str, ComparatorInput] = {c.generic_name: c for c in population.comparators}
    problems: Dict[str, str] = {}
    for s in assembly.pico_sets:
        allowed_tiers = set()
        for ref in (s.source_comparator_refs or [s.comparator]):
            comp = comparator_by_name.get(ref)
            if comp:
                allowed_tiers.update(comp.tiers)
        invented = set(s.tiers) - allowed_tiers
        if invented:
            problems[s.pico_set_id] = (
                f"Tier(s) {sorted(invented)} do not appear anywhere in Phase 1's original "
                f"comparator tiers for {s.source_comparator_refs or [s.comparator]} - "
                f"possible recalculation or invention, which must never happen.")
    return problems


def _check_rationale_present(assembly: SetAssemblyOutput) -> Dict[str, str]:
    return {
        s.pico_set_id: "Rationale is empty or generation failed"
        for s in assembly.pico_sets
        if not s.rationale.strip() or s.rationale.startswith("[RATIONALE GENERATION FAILED")
    }


# ---------------------------------------------------------------------------
# Semantic (LLM) checks
# ---------------------------------------------------------------------------

def _semantic_system_prompt() -> str:
    instruction_text = get_registry().get_active("validation").instruction_text
    return (
        instruction_text
        + "\n\n---\n\nFor THIS call, focus ONLY on the ONE specific PICO set described below. "
          "Deterministic checks (Member State completeness/duplication, tier consistency against "
          "Phase 1's original records, constant-field consistency, ordering, rationale non-emptiness) "
          "have ALREADY been run separately in Python and are not your task. Judge only: "
          "(1) is the rationale genuinely grounded in the comparator, the Member States it covers, "
          "and the evidence tier behind it - not a generic template, not a fabricated claim? "
          "(2) does the consolidation decision (combination_type / selection_basis) trace to an "
          "identifiable one of the guidance's four defined comparator scenarios, not an unexplained "
          "judgment call?\n\n"
          "Respond with ONLY this JSON object, nothing else:\n"
          '{"grounded": true|false, "traceable": true|false, "reason": "<if either is false, explain '
          'specifically which check failed and why; otherwise empty string>"}'
    )


def _semantic_check(pico_set: Phase2PicoSet, methodology_rule_text: str) -> _SemanticCheckOutput:
    system_prompt = _semantic_system_prompt()
    payload = {
        "comparator": pico_set.comparator,
        "member_states": pico_set.member_states,
        "tiers": pico_set.tiers,
        "combination_type": pico_set.combination_type,
        "selection_basis": pico_set.selection_basis,
        "rationale": pico_set.rationale,
    }
    user_prompt = (
        "LOCKED METHODOLOGY SUMMARY:\n" + methodology_rule_text
        + "\n\n---\n\nPICO SET TO VALIDATE (JSON):\n" + json.dumps(payload, indent=2, ensure_ascii=False)
    )
    # LLMCallFailed propagates to the caller (_work, in run_agent4) rather
    # than being swallowed here - the caller both builds the existing
    # fail-safe grounded=False/traceable=False object AND records a
    # structured RunLogEntry (added per explicit requirement: agent
    # failures must be logged with their cause).
    return invoke_structured(system_prompt, user_prompt, max_tokens=MAX_TOKENS_SEMANTIC_VALIDATION,
                              schema=_SemanticCheckOutput, retries=1)


def run_agent4(population: PopulationInstance, assembly: SetAssemblyOutput,
               methodology_rule_text: str) -> Tuple[ValidationOutput, List[RunLogEntry]]:
    ms_problem = _check_member_state_completeness(assembly)
    constants_problem = _check_constants(assembly)
    ordering_problem = _check_ordering(assembly)
    tier_problems = _check_tier_consistency(assembly, population)
    rationale_problems = _check_rationale_present(assembly)

    def _work(pico_set: Phase2PicoSet) -> Tuple[SetValidationResult, Optional[RunLogEntry]]:
        log_entry = None
        try:
            semantic = _semantic_check(pico_set, methodology_rule_text)
        except LLMCallFailed as exc:
            # Fail safely: an LLM failure here must BLOCK the set, never silently pass it.
            semantic = _SemanticCheckOutput(grounded=False, traceable=False,
                                             reason=f"Semantic validation call failed: {exc}")
            log_entry = RunLogEntry(
                timestamp=_now(), level="error", agent_id="validation",
                population=population.indication_disease, pico_set_id=pico_set.pico_set_id,
                error_type=type(exc).__name__, message=str(exc),
            )
        reasons = []
        # Cross-set/population-level problems are attached to every set in that
        # population, since the specific culprit set often cannot be identified
        # from a completeness/ordering failure alone (ENGINEERING DEFAULT - not
        # an SME requirement; a genuinely single-set-attributable failure like
        # tier invention or an empty rationale IS attached only to that set).
        if ms_problem:
            reasons.append(ms_problem)
        if constants_problem:
            reasons.append(constants_problem)
        if ordering_problem:
            reasons.append(ordering_problem)
        if pico_set.pico_set_id in tier_problems:
            reasons.append(tier_problems[pico_set.pico_set_id])
        if pico_set.pico_set_id in rationale_problems:
            reasons.append(rationale_problems[pico_set.pico_set_id])
        if not semantic.grounded:
            reasons.append(f"Rationale groundedness check failed: {semantic.reason}")
        if not semantic.traceable:
            reasons.append(f"Methodology traceability check failed: {semantic.reason}")

        passed = not reasons
        result = SetValidationResult(
            population=population.indication_disease,
            set_id=pico_set.pico_set_id,
            validation_result="passed" if passed else "failed_blocked",
            failure_reason=None if passed else "; ".join(reasons),
        )
        return result, log_entry

    logs: List[RunLogEntry] = []
    if assembly.pico_sets:
        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_VALIDATION_CALLS, len(assembly.pico_sets))) as pool:
            worked = list(pool.map(_work, assembly.pico_sets))
        results = [result for result, _ in worked]
        logs = [log for _, log in worked if log is not None]
    else:
        results = []

    total_accounted = len(
        {ms for s in assembly.pico_sets for ms in s.member_states} | set(assembly.not_identified_states))
    matches = (total_accounted == 27 and ms_problem is None)

    validation = ValidationOutput(
        results=results,
        member_state_count_check=MemberStateCountCheck(total=27, matches=matches),
    )
    return validation, logs
