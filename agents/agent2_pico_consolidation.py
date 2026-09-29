"""Agent 2 - PICO Consolidation.

The core LLM judgment task (SME's own framing: "the single most consequential
judgment task in this workflow"). The 4-case scenario resolution and tie-
breaking are genuinely LLM work (natural-language comparator matching,
exact-match/partial-overlap judgment); the mechanical cross-state attachment
that follows is deterministic Python (cross_state_attachment.py) and is
never re-asked of the LLM, per the task's explicit instruction (Section 10).
"""

from __future__ import annotations

import json

from p2_config import EU_27_MEMBER_STATES, MAX_TOKENS_PICO_CONSOLIDATION
from cross_state_attachment import attach_cross_state_members
from llm_client import invoke_structured
from prompts.prompt_builder import build_system_prompt
from schemas import PicoConsolidationOutput, PopulationInstance


def _member_state_entry(r) -> dict:
    """`retain_all_status` is only a meaningful concept for the at_least_one
    scenario (it IS the 3a/3b sub-case distinction) - per the actual HTA
    guidance (Figure 3), unique/each_required/individualised have no
    droppability dimension at all. Phase 1's schema always populates
    retain_all_status with a default value ("unconfirmed") regardless of
    scenario, purely as a dataclass default - it is not a real fact for
    non-at_least_one entries. Omitting it here for those entries prevents
    the LLM from echoing a meaningless default into `droppable_note` on a
    unique/each_required/individualised selection (found via a real
    end-to-end Bedrock run during implementation - Agent 4's semantic check
    correctly caught the resulting internal contradiction)."""
    entry = {"member_state": r.member_state, "comparator_scenario": r.comparator_scenario.value, "tier": r.tier}
    if r.comparator_scenario.value == "at_least_one":
        entry["retain_all_status"] = r.retain_all_status.value
    return entry


def _build_user_prompt(population: PopulationInstance, methodology_rule_text: str) -> str:
    comparators_payload = [
        {
            "generic_name": c.generic_name,
            "class_or_mechanism": c.class_or_mechanism,
            "line_of_therapy": c.line_of_therapy,
            "per_member_state": [_member_state_entry(r) for r in c.per_member_state],
            "tiers": c.tiers,
        }
        for c in population.comparators
    ]
    payload = {
        "population": population.indication_disease,
        "population_label": population.population_label,
        "eu_27_member_states": EU_27_MEMBER_STATES,
        "comparators": comparators_payload,
    }
    return (
        "LOCKED METHODOLOGY (apply exactly, per your instructions above):\n\n"
        f"{methodology_rule_text}\n\n---\n\nCONSOLIDATION INPUT (JSON):\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
        "Apply the locked methodology to this population's confirmed comparator data and "
        "produce the required JSON output. For every selected_comparators[] entry, populate "
        "source_comparator_refs with the exact generic_name(s) of every input comparator that "
        "make up this selection."
    )


def _clear_droppable_note_where_not_applicable(selected):
    """Deterministic safeguard (task Section 1: engineering-level schema
    enforcement is permitted around the SME's business methodology): per the
    guidance, droppability is only a real concept for the at_least_one
    scenario. Even with the input-side fix above, force droppable_note back
    to None for any other selection_basis rather than trusting the LLM
    never repeats the mistake - never silently generating an internally
    inconsistent set (task Section 31, no-hallucination policy)."""
    updated = []
    for sel in selected:
        if sel.selection_basis != "at_least_one" and sel.droppable_note is not None:
            sel = sel.model_copy(update={"droppable_note": None})
        updated.append(sel)
    return updated


def run_agent2(population: PopulationInstance, methodology_rule_text: str) -> PicoConsolidationOutput:
    if not population.comparators:
        return PicoConsolidationOutput(population=population.indication_disease, selected_comparators=[])

    system_prompt = build_system_prompt("pico_consolidation")
    user_prompt = _build_user_prompt(population, methodology_rule_text)
    result = invoke_structured(
        system_prompt, user_prompt, max_tokens=MAX_TOKENS_PICO_CONSOLIDATION,
        schema=PicoConsolidationOutput, retries=1,
    )
    result.selected_comparators = _clear_droppable_note_where_not_applicable(result.selected_comparators)
    result.selected_comparators = attach_cross_state_members(result.selected_comparators, population.comparators)
    return result
