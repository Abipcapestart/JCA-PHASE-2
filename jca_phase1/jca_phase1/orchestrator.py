"""
Phase 1 orchestrator — the single entry point.

There is exactly ONE run path. The legacy codebase had a main orchestrator plus
two "resume and patch" scripts that rewrote part of a saved run; one of them
rewrote the comparator list without re-deriving the downstream view, which is
how a deliverable shipped with sections that disagreed with each other. Resume
here re-runs from a checkpoint through the SAME code, or it does not resume.

Stage order (see ARCHITECTURE.md for the diagram):
  A1 validate -> A2 structure -> [user confirms] -> A4 indication lock ->
  A3 scope boundary -> A5 areas -> A6 query plan -> A7 retrieve ->
  A8 extract -> A9 ground -> A10 validate claims -> A11 identity ->
  A12 adjudicate -> A14 harmonise -> A13/A15 assign+catalog -> A16 consolidate ->
  A17 completeness
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import config as C
from .agents import a01_a05_input_context as inputs
from .agents import a06_a08_retrieval as retrieval
from .agents import a09_a13_validation as validation
from .agents import a14_a18_consolidation as consolidation
from .providers.llm import LLMClient
from .providers.registries import (LiteratureClient, MedicineRegistryClient,
                                   TrialRegistryClient)
from .providers.search import SearchProvider
from .prompts import registry as prompt_registry
from .schema import (EvidenceRecord, Intervention, ORIGIN_AGENT, Phase1Output,
                     Population, SourceClassAttempt, ValidationSummary)
from .sources.workbook import SourceInventory, load_source_inventory


@dataclass
class Providers:
    """Everything the pipeline talks to. All optional: a provider that is None
    degrades that capability explicitly rather than failing the run."""
    llm: Optional[LLMClient] = None
    search: Optional[SearchProvider] = None
    trials: Optional[TrialRegistryClient] = None
    literature: Optional[LiteratureClient] = None
    medicines: Optional[MedicineRegistryClient] = None


@dataclass
class RunOptions:
    request_id: str = ""
    allow_jca_reports: bool = False      # evaluation only; recorded in the manifest
    source_workbook: Optional[str] = None
    strict_sources: bool = True
    max_workers: int = C.RETRIEVAL.max_workers
    skip_confirmation: bool = True       # API drives confirmation; CLI auto-confirms
    progress: Optional[Callable[[str, str], None]] = None

    def emit(self, stage: str, message: str) -> None:
        if self.progress:
            try:
                self.progress(stage, message)
            except Exception:  # noqa: BLE001 — progress must never break a run
                pass


class BlockedError(RuntimeError):
    """Raised when P&I validation returns a FIX. The SME is explicit that the
    user cannot proceed past this stage while an unresolved FIX remains."""

    def __init__(self, result):
        super().__init__("P&I validation returned blocking FIX items")
        self.validation = result


def _snapshot(debug_capture: Optional[Dict[str, Any]], key: str, value: Any) -> None:
    if debug_capture is not None:
        debug_capture[key] = value


def _doc_snapshot(doc, max_chars: int = 3000) -> Dict[str, Any]:
    """RetrievedDocument has no to_dict(); asdict() plus a text cap so a debug
    capture with dozens of full documents doesn't balloon the run file."""
    d = asdict(doc)
    if len(d.get("text", "")) > max_chars:
        d["text"] = d["text"][:max_chars] + f"... [truncated, {len(doc.text)} chars total]"
    return d


def run_phase1(population_input: Dict[str, Any],
               intervention_input: Dict[str, Any],
               providers: Providers,
               free_text: str = "",
               added_fields: Optional[List[str]] = None,
               options: Optional[RunOptions] = None,
               debug_capture: Optional[Dict[str, Any]] = None) -> Phase1Output:
    """`debug_capture`, when passed an empty dict, is filled in-place with a
    raw snapshot of every stage's intermediate state (every extracted record
    before/after grounding and validation, every retrieval attempt, every
    candidate comparator's scope adjudication including the ones that did NOT
    make the final cut) -- for debugging why something is or isn't in the
    final output. None by default: existing callers see no change at all."""
    opts = options or RunOptions()
    started = time.time()
    out = Phase1Output(request_id=opts.request_id or f"jca-{uuid.uuid4().hex[:10]}")
    out.raw_population_input = dict(population_input)
    out.raw_intervention_input = dict(intervention_input)

    # ---- A1 ---------------------------------------------------------------
    opts.emit("A1", "Validating population and intervention fields")
    pi_validation = inputs.validate_pi(population_input, intervention_input,
                                       added_fields, providers.llm)
    out.input_validation = pi_validation.to_dict()
    if not pi_validation.passed:
        raise BlockedError(pi_validation)

    # ---- A2 ---------------------------------------------------------------
    opts.emit("A2", "Structuring input and tagging provenance")
    populations, intervention = inputs.structure_pi(
        population_input, intervention_input, free_text, providers.llm)
    out.populations = populations
    out.intervention = intervention

    # ---- A4 ---------------------------------------------------------------
    opts.emit("A4", "Establishing the licensed indication and leakage guard")
    indication_record, guard = inputs.lock_indication(
        intervention, providers.medicines, providers.search, providers.llm,
        allow_jca_reports=opts.allow_jca_reports)
    out.licensed_indication_record = indication_record
    intervention.inn_resolved = indication_record.inn or intervention.product_name
    intervention.atc_code = indication_record.atc_code

    # ---- A3 ---------------------------------------------------------------
    opts.emit("A3", "Building the population scope boundary")
    boundaries = [inputs.build_scope_boundary(p, intervention, indication_record,
                                              providers.llm)
                  for p in populations]
    out.scope_boundaries = boundaries

    # ---- A5 ---------------------------------------------------------------
    opts.emit("A5", "Resolving therapeutic area(s)")
    areas = inputs.resolve_therapeutic_areas(populations[0], providers.llm)
    out.therapeutic_areas = areas

    # ---- source inventory -------------------------------------------------
    opts.emit("sources", "Loading the curated source list")
    inventory = load_source_inventory(opts.source_workbook, strict=opts.strict_sources)
    for p in inventory.problems:
        out.notes.append(f"[source-list {p.severity}] {p.where}: {p.message}")

    # ---- A6 ---------------------------------------------------------------
    opts.emit("A6", "Planning retrieval queries")
    vocab = retrieval.build_vocabulary(populations[0], areas.areas, providers.llm)
    plan = retrieval.plan_queries(populations[0], intervention, areas.areas,
                                  vocab, inventory)
    opts.emit("A6", f"{len(plan)} query units planned across "
                    f"{len({i.member_state for i in plan})} scopes")
    _snapshot(debug_capture, "A6_query_plan", {
        "vocabulary": vocab.to_dict(),
        "plan": [asdict(p) for p in plan]})

    # ---- A7 ---------------------------------------------------------------
    records: List[EvidenceRecord] = []
    attempts: List[SourceClassAttempt] = []
    documents = []

    if providers.search is not None:
        opts.emit("A7", "Retrieving documents")
        web = retrieval.execute_plan(plan, providers.search, guard, inventory,
                                     areas.areas, opts.max_workers)
        documents = web.documents
        attempts.extend(web.attempts)
        if web.blocked_by_guard:
            out.notes.append(
                f"[leakage guard] {len(web.blocked_by_guard)} document(s) blocked as the "
                f"target drug's own JCA report. This is the intended behaviour: the "
                f"product must anticipate a scope, not read the answer key.")
    else:
        out.notes.append("[A7] No search provider configured; web retrieval skipped.")

    opts.emit("A7", "Querying structured registries")
    structured = retrieval.retrieve_structured(
        populations[0], intervention, indication_record,
        providers.trials, providers.literature, vocab)
    attempts.extend(structured.attempts)
    _snapshot(debug_capture, "A7_retrieval", {
        "attempts": [a.to_dict() for a in attempts],
        "documents": [_doc_snapshot(d) for d in documents],
        "trials_found": [asdict(t) for t in structured.trials],
        "publications_found": [asdict(p) for p in getattr(structured, "publications", [])]})

    # ---- A8 ---------------------------------------------------------------
    if providers.llm is not None and documents:
        opts.emit("A8", f"Extracting evidence from {len(documents)} document(s)")
        records.extend(retrieval.extract_from_documents(
            documents, populations[0], intervention, providers.llm, opts.max_workers))
    records.extend(retrieval.records_from_trials(structured.trials, intervention))
    out.validation.records_extracted = len(records)
    _snapshot(debug_capture, "A8_extraction", [r.to_dict() for r in records])

    # ---- A9 ---------------------------------------------------------------
    opts.emit("A9", "Grounding extracted values in their source text")
    records = validation.ground_records(records, documents)
    out.validation.grounded = sum(1 for r in records if r.grounded)
    out.validation.grounding_failed = len(records) - out.validation.grounded
    _snapshot(debug_capture, "A9_grounding", [r.to_dict() for r in records])

    # ---- A10 --------------------------------------------------------------
    if providers.llm is not None and providers.search is not None:
        opts.emit("A10", "Re-opening cited sources and validating claims")
        records = validation.validate_claims(
            records, populations[0].value("indication_disease"), intervention,
            providers.search, providers.llm, opts.max_workers)
    else:
        out.notes.append("[A10] Claim validation skipped: it requires both an LLM and a "
                         "search provider, because it re-fetches the cited source.")
    _tally_validation(records, out.validation)
    _snapshot(debug_capture, "A10_claim_validation", [r.to_dict() for r in records])

    usable = [r for r in records if r.usable()]
    _snapshot(debug_capture, "usable_after_A10", [r.to_dict() for r in usable])

    # ---- A11 --------------------------------------------------------------
    opts.emit("A11", "Resolving comparator substance identity")
    identities = validation.resolve_identities(usable, providers.llm)
    validation.apply_identities(usable, identities)
    _snapshot(debug_capture, "A11_identity", {k: asdict(v) for k, v in identities.items()})

    # ---- A14 (grouping) ---------------------------------------------------
    opts.emit("A14", "Harmonising comparator identities")
    groups = consolidation.group_comparators(
        [r for r in usable if r.finding_type == "comparator"])
    _snapshot(debug_capture, "A14_grouping", {
        key: [r.to_dict() for r in recs] for key, recs in groups.items()})

    # ---- A12 --------------------------------------------------------------
    opts.emit("A12", f"Adjudicating scope for {len(groups)} candidate comparator(s)")
    boundary = boundaries[0]
    searched_states = {a.member_state for a in attempts
                       if a.attempted and a.member_state in C.EU_27_MEMBER_STATES}
    in_scope: Dict[str, Any] = {}
    all_adjudications: Dict[str, Any] = {}
    for key, recs in groups.items():
        comp = recs[0].comparator
        adj = validation.adjudicate_scope(key, comp, recs, boundary, intervention,
                                          providers.llm)
        all_adjudications[key] = {"comparator": comp.to_dict(), "adjudication": adj.to_dict(),
                                  "num_records": len(recs)}
        if adj.verdict == C.SCOPE_IN:
            out.validation.scope_in += 1
            in_scope[key] = (comp, recs, adj)
        elif adj.verdict == C.SCOPE_UNCERTAIN:
            out.validation.scope_uncertain += 1
            in_scope[key] = (comp, recs, adj)   # surfaced, never silently dropped
        else:
            out.validation.scope_out += 1
            out.validation.excluded.append({
                "value": comp.as_stated, "stage": "scope_adjudication",
                "reason": adj.reason, "decisive_facet": adj.decisive_facet})
    # Every candidate, in scope or not -- this is the one place to see why a
    # real comparator mention never reached the final output.
    _snapshot(debug_capture, "A12_scope_adjudication", all_adjudications)

    # ---- A16 / A13 --------------------------------------------------------
    opts.emit("A16", "Consolidating comparators")
    comparators = []
    dropped_at_consolidation = []
    for key, (comp, recs, adj) in in_scope.items():
        try:
            comparators.append(consolidation.build_comparator(
                key, comp, recs, adj, searched_states, providers.llm))
        except ValueError as exc:
            out.notes.append(f"[A16] {comp.as_stated!r} dropped: {exc}")
            dropped_at_consolidation.append({"value": comp.as_stated, "reason": str(exc)})
    _snapshot(debug_capture, "A16_dropped_at_consolidation", dropped_at_consolidation)
    or_map = consolidation.detect_or_alternatives(comparators)
    for c in comparators:
        c.or_alternative = any(len(or_map.get(s, [])) > 1 for s in c.member_states)
    comparators.sort(key=lambda c: (-len(c.member_states), c.generic_name.lower()))
    out.comparators = comparators

    # ---- A15 --------------------------------------------------------------
    opts.emit("A15", "Harmonising outcomes and checking catalog coverage")
    outcome_groups = consolidation.harmonize_outcomes(
        [r for r in usable if r.finding_type == "outcome"], providers.llm)
    _snapshot(debug_capture, "A15_outcome_groups", [
        {"concept": concept, "catalog_id": catalog_id, "records": [r.to_dict() for r in recs]}
        for concept, catalog_id, recs in outcome_groups])
    out.outcomes = consolidation.build_outcomes(outcome_groups, providers.llm)

    # ---- by-state view ----------------------------------------------------
    entries, not_identified, summary = consolidation.build_by_member_state(
        comparators, attempts)
    out.by_member_state = entries
    out.not_identified_states = not_identified
    out.member_state_summary = summary

    # ---- source registry --------------------------------------------------
    seen = set()
    for c in comparators:
        for s in c.sources:
            if s.source_id not in seen:
                seen.add(s.source_id)
                out.source_registry.append(s)
    for o in out.outcomes.all_outcomes():
        for s in o.sources:
            if s.source_id not in seen:
                seen.add(s.source_id)
                out.source_registry.append(s)

    # ---- A17 --------------------------------------------------------------
    opts.emit("A17", "Auditing completeness")
    out.completeness = consolidation.audit_completeness(
        out, attempts, [p.population_id for p in populations])

    out.run_manifest = _manifest(out, providers, inventory, guard, started, opts)
    opts.emit("done", f"{len(out.comparators)} comparator(s), "
                      f"{len(out.outcomes.all_outcomes())} outcome(s)")
    return out


def _tally_validation(records: Sequence[EvidenceRecord], summary: ValidationSummary) -> None:
    for r in records:
        v = r.validation
        if v.refetched:
            summary.refetch_attempted += 1
        if v.verdict == C.V_WRONG_SUBJECT_DRUG:
            summary.subject_drug_rejections += 1
        if v.verdict == C.V_WRONG_INTERVENTION:
            summary.role_rejections += 1
        if v.passed:
            summary.claim_validated += 1
        elif v.verdict:
            summary.claim_rejected += 1
            value = (r.comparator.as_stated if r.comparator
                     else (r.outcome.measure if r.outcome else ""))
            summary.excluded.append({"value": value, "stage": "claim_validation",
                                     "reason": v.reason, "source_url": r.source_url})


def _manifest(out: Phase1Output, providers: Providers, inventory: SourceInventory,
              guard, started: float, opts: RunOptions) -> Dict[str, Any]:
    """Everything needed to reproduce and explain this run.

    MAI-34392 requires the model and the source list in the audit trail. Prompt
    versions belong there too: a run that cannot be traced to the prompt text
    that produced it cannot be explained when it is wrong.
    """
    import os
    manifest: Dict[str, Any] = {
        "request_id": out.request_id,
        "phase": "phase1_comparator_outcome_scoping",
        "phase2_included": False,
        "code_version": os.getenv("JCA_CODE_VERSION", "dev"),
        "model": C.LLM.model_id if providers.llm else None,
        "prompt_versions": prompt_registry.versions(),
        "outcome_catalog_version": consolidation.CATALOG["catalog_version"],
        "source_workbook": {
            "path": inventory.path,
            "sheets_matched": inventory.sheets_matched,
            "entries": len(inventory.entries),
            "unique_domains": len(inventory.unique_domains),
            "problems": [p.to_dict() for p in inventory.problems],
            "mtime": (datetime.fromtimestamp(os.path.getmtime(inventory.path),
                                             timezone.utc).isoformat()
                      if inventory.path and os.path.exists(inventory.path) else None),
        },
        "therapeutic_areas_applied": out.therapeutic_areas.areas,
        "leakage_guard": guard.to_dict(),
        "tier3_enabled": C.ENABLE_TIER_3_GENERAL_WEB,
        "providers": {
            "llm": type(providers.llm).__name__ if providers.llm else None,
            "search": type(providers.search).__name__ if providers.search else None,
            "trials": type(providers.trials).__name__ if providers.trials else None,
            "literature": type(providers.literature).__name__ if providers.literature else None,
            "medicines": type(providers.medicines).__name__ if providers.medicines else None,
        },
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.time() - started, 2),
    }
    if providers.llm:
        manifest["llm_usage"] = providers.llm.usage_summary()
    if providers.search:
        manifest["search_usage"] = providers.search.usage_summary()
    return manifest
