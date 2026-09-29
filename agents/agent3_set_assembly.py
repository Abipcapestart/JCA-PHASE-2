"""Agent 3 - PICO Set Assembly.

Per task Section 12: deterministic crossing/attachment/ordering/constants in
Python; the LLM is used ONLY for the one genuinely semantic task the SME
prompt assigns this agent - writing each set's fresh rationale. The SME's
full Agent 3 instruction text is still the system prompt for the rationale
call (business methodology preserved verbatim), with one engineering-level
scoping note appended explaining that the deterministic steps are already
done and only the rationale is being requested for this call - never a
change to the SME's business rules themselves.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from p2_config import MAX_CONCURRENT_RATIONALE_CALLS, MAX_TOKENS_RATIONALE
from llm_client import LLMCallFailed, invoke_structured
from prompts.registry import get_registry
from schemas import (
    ComparatorInput, Phase2PicoSet, PicoConsolidationOutput, PopulationInstance, RunLogEntry, SetAssemblyOutput,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _RationaleOutput(BaseModel):
    rationale: str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def _rationale_system_prompt() -> str:
    instruction_text = get_registry().get_active("set_assembly").instruction_text
    return (
        instruction_text
        + "\n\n---\n\nFor THIS call, the deterministic assembly steps (crossing the comparator with "
          "the population, attaching Member States, attaching the constant intervention/outcomes, "
          "carrying forward tiers, ordering sets, compiling the not-identified list) have ALREADY "
          "been performed in Python and are not your task. Your ONLY task for this call is to write "
          "the rationale for the ONE specific PICO set described below, following the rationale-"
          "writing rules above (Generation Process steps 5-6, and the relevant Self-Validation "
          "Checklist items).\n\n"
          "Respond with ONLY this JSON object, nothing else - no preamble, no markdown fences:\n"
          '{"rationale": "<one or two concise sentences>"}'
    )


def _generate_rationale(pico_set: Phase2PicoSet, grounding_rationale: str, droppable_note: str) -> str:
    system_prompt = _rationale_system_prompt()
    payload = {
        "comparator": pico_set.comparator,
        "member_states": pico_set.member_states,
        "member_state_count": len(pico_set.member_states),
        "tiers": pico_set.tiers,
        "combination_type": pico_set.combination_type,
        "selection_basis": pico_set.selection_basis,
        "droppable_note": droppable_note or None,
    }
    user_prompt = (
        "PICO SET FACTS (JSON):\n" + json.dumps(payload, indent=2, ensure_ascii=False)
        + "\n\nSCOPING'S ORIGINAL CLINICAL RATIONALE FOR THIS COMPARATOR (grounding material - "
          "read, do not copy verbatim):\n" + (grounding_rationale or "(none available)")
        + "\n\nWrite the fresh rationale for this set now."
    )
    # LLMCallFailed propagates to the caller (_work, in run_agent3) rather
    # than being swallowed here - it needs to reach the caller both to
    # produce the existing marker-text fallback AND to record a structured
    # RunLogEntry (added per explicit requirement: agent failures must be
    # logged with their cause, not just degrade silently into marker text).
    result = invoke_structured(system_prompt, user_prompt, max_tokens=MAX_TOKENS_RATIONALE,
                                schema=_RationaleOutput, retries=1)
    return result.rationale


def run_agent3(population: PopulationInstance, consolidation: PicoConsolidationOutput,
                intervention_name: str = "") -> Tuple[SetAssemblyOutput, List[RunLogEntry]]:
    """`intervention_name` must be passed by the caller (graph.py, from
    `Phase2Input.intervention.product_name`) - `population.attributes` never
    carries a `product_name` key from the real adapter (only age_group/sex/
    stage_severity/... population-level attributes; see adapter.py's
    `_adapt_population`). A prior version of this function read
    `population.attributes.get("product_name")`, which is always empty in
    every real run - only test helpers happened to fake that exact key,
    which is how this went unnoticed. Fixed here, not by adding a fake key
    to the adapter, since product_name is genuinely a Phase2Input-level
    fact, not a per-population one."""
    comparator_by_name: Dict[str, ComparatorInput] = {c.generic_name: c for c in population.comparators}
    outcome_names = [o.concept for o in population.outcomes]

    draft_sets: List[Phase2PicoSet] = []
    grounding_by_set: Dict[str, str] = {}
    droppable_by_set: Dict[str, str] = {}

    for sel in consolidation.selected_comparators:
        refs = sel.source_comparator_refs or [sel.generic_name]
        grounding_texts = [comparator_by_name[r].rationale for r in refs
                            if r in comparator_by_name and comparator_by_name[r].rationale]
        pico_set_id = _slugify(sel.generic_name)
        draft = Phase2PicoSet(
            pico_set_id=pico_set_id,
            comparator=sel.generic_name,
            intervention=intervention_name,
            outcomes=outcome_names,
            member_states=sel.member_states,
            tiers=sel.tiers,
            rationale="",  # filled below
            combination_type=sel.combination_type,
            selection_basis=sel.selection_basis,
            tie_break=sel.tie_break,
            source_comparator_refs=refs,
        )
        draft_sets.append(draft)
        grounding_by_set[pico_set_id] = " ".join(grounding_texts)
        droppable_by_set[pico_set_id] = sel.droppable_note or ""

    # Bounded concurrency for rationale generation - never unbounded LLM calls (task Section 13/17).
    def _work(pico_set: Phase2PicoSet) -> Tuple[Phase2PicoSet, Optional[RunLogEntry]]:
        try:
            rationale = _generate_rationale(
                pico_set, grounding_by_set[pico_set.pico_set_id], droppable_by_set[pico_set.pico_set_id])
            return pico_set.model_copy(update={"rationale": rationale}), None
        except LLMCallFailed as exc:
            # Fail safely (task Section 18/31): never fabricate a rationale -
            # Agent 4's deterministic "rationale must be non-empty" check
            # blocks this specific set for review. Also recorded as a
            # structured log entry so the cause is visible, not just the
            # marker text.
            marker = f"[RATIONALE GENERATION FAILED - flagged for review: {exc}]"
            log_entry = RunLogEntry(
                timestamp=_now(), level="error", agent_id="set_assembly",
                population=population.indication_disease, pico_set_id=pico_set.pico_set_id,
                error_type=type(exc).__name__, message=str(exc),
            )
            return pico_set.model_copy(update={"rationale": marker}), log_entry

    logs: List[RunLogEntry] = []
    if draft_sets:
        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_RATIONALE_CALLS, len(draft_sets))) as pool:
            worked = list(pool.map(_work, draft_sets))
        final_sets = [pico_set for pico_set, _ in worked]
        logs = [log for _, log in worked if log is not None]
    else:
        final_sets = []

    # Broadest-first ordering (deterministic, task Section 12).
    final_sets.sort(key=lambda s: len(s.member_states), reverse=True)

    assembly = SetAssemblyOutput(
        population=population.indication_disease,
        pico_sets=final_sets,
        not_identified_states=list(population.not_identified_states),
    )
    return assembly, logs
