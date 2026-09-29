"""LangGraph orchestration for Phase 2 - PICOS Set Consolidation.

Graph shape (see PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md Section 9/11):

    input validation (deterministic, at adapter time - not a graph node here,
                       since Phase2Input is already validated by the time it
                       reaches this graph)
        |
        v
    context_locking          (Agent 1 - deterministic)
        |
        v
    process_populations      (bounded-concurrency fan-out over populations;
                               each population runs Agent 2 -> Agent 3 ->
                               Agent 4 sequentially - Agent 3/4 themselves
                               fan out per-PICO-set with their own bounded
                               concurrency, see agents/agent3_*.py/agent4_*.py)
        |
        v
    END

Population-level fan-out is implemented as bounded-concurrency Python
inside one node rather than LangGraph's Send() API - this JCA workspace's
installed LangGraph version's Send/fan-out API was not verified against a
pinned version before this implementation, and a node-internal
ThreadPoolExecutor is an equally valid, simpler, version-safe way to satisfy
the "bounded per-population parallelism" requirement (task Section 11/17)
without risking an API mismatch. Still a genuine StateGraph, still fully
observable/testable per node.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, StateGraph

from agents.agent1_context_locking import run_agent1
from agents.agent2_pico_consolidation import run_agent2
from agents.agent3_set_assembly import run_agent3
from agents.agent4_validation import run_agent4
from p2_config import BEDROCK_MODEL_ID, MAX_CONCURRENT_POPULATIONS
from methodology import load_methodology
from prompts.prompt_builder import active_prompt_version
from prompts.registry import AGENT_IDS
from schemas import ContextLockingOutput, Phase2Input, Phase2RunResult, PopulationResult, RunLogEntry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState(TypedDict, total=False):
    phase2_input: Phase2Input
    context_locking: ContextLockingOutput
    methodology_rule_text: str
    population_results: List[PopulationResult]
    logs: List[RunLogEntry]


def _node_context_locking(state: PipelineState) -> Dict[str, Any]:
    logs = list(state.get("logs", []))
    try:
        ctx = run_agent1()
        rule_text = load_methodology().rule_text
    except Exception as exc:  # noqa: BLE001 - Agent 1 must never crash the whole run uncaught
        # A prior version let a missing/unparseable guidance PDF raise
        # straight out of run_pipeline() - the whole run would crash with no
        # Phase2RunResult at all, unlike every other agent's designed
        # graceful degradation. Fixed: record the cause, return a run that's
        # explicitly BLOCKED (via the empty ContextLockingOutput below,
        # which _derive_overall_status on Phase2RunResult picks up from this
        # error-level log entry) instead of crashing.
        ctx = ContextLockingOutput(methodology_loaded=False, guidance_version="unknown", sections_covered=[])
        rule_text = ""
        logs.append(RunLogEntry(
            timestamp=_now(), level="error", agent_id="context_locking",
            error_type=type(exc).__name__, message=str(exc),
        ))
    else:
        if not ctx.methodology_loaded:
            logs.append(RunLogEntry(
                timestamp=_now(), level="error", agent_id="context_locking", error_type="MethodologyIncomplete",
                message=(
                    f"Could not confirm the HTA-JCA guidance methodology loaded in full "
                    f"({len(ctx.sections_covered)}/4 sections confirmed) - downstream consolidation/"
                    f"validation may be operating on an incomplete methodology."),
            ))
    return {"context_locking": ctx, "methodology_rule_text": rule_text, "logs": logs}


def _process_one_population(
    population, methodology_rule_text: str, intervention_name: str,
) -> Tuple[Optional[PopulationResult], List[RunLogEntry]]:
    logs: List[RunLogEntry] = []
    stage = "pico_consolidation"
    try:
        consolidation = run_agent2(population, methodology_rule_text)
        stage = "set_assembly"
        assembly, assembly_logs = run_agent3(population, consolidation, intervention_name)
        logs.extend(assembly_logs)
        stage = "validation"
        validation, validation_logs = run_agent4(population, assembly, methodology_rule_text)
        logs.extend(validation_logs)
        return PopulationResult(
            population=population.indication_disease,
            population_label=population.population_label,
            consolidation=consolidation,
            assembly=assembly,
            validation=validation,
        ), logs
    except Exception as exc:  # noqa: BLE001 - must never crash the whole run for one population
        logs.append(RunLogEntry(
            timestamp=_now(), level="error", agent_id=stage,
            population=population.population_display_name,
            error_type=type(exc).__name__, message=str(exc),
        ))
        return None, logs


def _node_process_populations(state: PipelineState) -> Dict[str, Any]:
    phase2_input = state["phase2_input"]
    methodology_rule_text = state.get("methodology_rule_text", "")
    logs = list(state.get("logs", []))

    if not methodology_rule_text:
        # Only reachable via _node_context_locking's except branch (the
        # "not fully confirmed" warning case still carries the full
        # hardcoded rule_text - see methodology.py). Proceeding here would
        # send every population's Agent 2/4 calls out with no locked
        # methodology at all, silently producing ungrounded output - skip
        # entirely instead; the run is already logged as BLOCKED.
        logs.append(RunLogEntry(
            timestamp=_now(), level="error", agent_id="pico_consolidation", error_type="MethodologyNotLoaded",
            message="Skipped all population processing - the locked methodology text is empty "
                    "(Agent 1 did not load successfully, see the context_locking log entry above).",
        ))
        return {"population_results": [], "logs": logs}

    populations = phase2_input.populations
    intervention_name = phase2_input.intervention.product_name
    results: List[PopulationResult] = []

    worker_count = max(1, min(MAX_CONCURRENT_POPULATIONS, len(populations)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_process_one_population, pop, methodology_rule_text, intervention_name)
                   for pop in populations]
        for future in futures:
            result, pop_logs = future.result()
            if result is not None:
                results.append(result)
            logs.extend(pop_logs)

    return {"population_results": results, "logs": logs}


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("context_locking", _node_context_locking)
    graph.add_node("process_populations", _node_process_populations)
    graph.set_entry_point("context_locking")
    graph.add_edge("context_locking", "process_populations")
    graph.add_edge("process_populations", END)
    return graph.compile()


def run_pipeline(phase2_input: Phase2Input) -> Phase2RunResult:
    app = build_graph()
    initial_state: PipelineState = {"phase2_input": phase2_input, "logs": []}
    final_state = app.invoke(initial_state)

    prompt_versions = {agent_id: active_prompt_version(agent_id) for agent_id in AGENT_IDS}

    return Phase2RunResult(
        run_id=phase2_input.run_id,
        model=BEDROCK_MODEL_ID,
        prompt_versions=prompt_versions,
        context_locking=final_state["context_locking"],
        populations=final_state.get("population_results", []),
        logs=final_state.get("logs", []),
    )
