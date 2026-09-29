"""
A6  Query Planning      — deterministic templates + ONE vocabulary LLM call
A7  Source-routed retrieval — APIs for structured sources, search for the rest
A8  Evidence Extraction — one typed contract shared by every source class

Design decisions this module encodes, each traceable to a measured failure:

* Queries are composed by CODE from a typed vocabulary object. The vocabulary
  call cannot name a drug, so query planning structurally cannot decide the
  answer.
* Every per-state guideline search runs TWICE: drug-anchored, and a LANDSCAPE
  pass that deliberately omits the drug. A guideline listing the complete
  treatment-line landscape rarely ranks for a query anchored to one drug, which
  is the confirmed cause of missed guideline comparators.
* Guideline domains are filtered by therapeutic area. Without it ~89% of the
  domains offered per state are the wrong specialty.
* Trials and literature go to APIs. A trial's comparator is a typed field.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config as C
from ..providers.llm import LLMClient
from ..providers.registries import (LiteratureClient, PublicationRecord,
                                    TrialRecord, TrialRegistryClient)
from ..providers.search import SearchHit, SearchProvider, select_balanced
from ..schema import (Comparator, EvidenceRecord, FINDING_COMPARATOR, FINDING_OUTCOME,
                      Intervention, LeakageGuard, LicensedIndicationRecord,
                      OutcomeMention, PASS_DRUG_ANCHORED, PASS_LANDSCAPE,
                      PASS_OUTCOME_REQUIREMENT, PASS_REFINEMENT, Population,
                      PopulationContext, QueryPlanItem, QueryVocabulary,
                      RetrievedDocument, ScopeBoundary, SourceClassAttempt)
from ..sources.workbook import SourceInventory, normalise_domain

# Languages worth localising HTA queries into, keyed by Member State. Only the
# states whose bodies routinely publish in their own language.
STATE_LANGUAGE: Dict[str, str] = {
    "Germany": "de", "Austria": "de", "France": "fr", "Belgium": "fr",
    "Luxembourg": "fr", "Italy": "it", "Spain": "es", "Portugal": "pt",
    "Netherlands": "nl", "Denmark": "da", "Sweden": "sv", "Finland": "fi",
    "Poland": "pl", "Czech Republic": "cs", "Slovakia": "sk", "Hungary": "hu",
    "Romania": "ro", "Bulgaria": "bg", "Greece": "el", "Croatia": "hr",
    "Slovenia": "sl", "Estonia": "et", "Latvia": "lv", "Lithuania": "lt",
}


# ===========================================================================
# A6 — Query planning
# ===========================================================================

def build_vocabulary(population: Population, areas: Sequence[str],
                     llm: Optional[LLMClient] = None) -> QueryVocabulary:
    """ONE LLM call per request producing terminology only.

    The output type has no comparator field. That is the guard: a component
    that cannot represent a comparator cannot leak one into retrieval.
    """
    indication = population.value("indication_disease")
    vocab = QueryVocabulary(indication_synonyms=[indication] if indication else [])
    if llm is None or not indication:
        return vocab

    languages = sorted({lang for lang in STATE_LANGUAGE.values()})
    payload = (f"INDICATION: {indication}\n"
               f"DISEASE SUBTYPE: {population.value('disease_subtype_histology') or '(not provided)'}\n"
               f"THERAPEUTIC AREA(S): {', '.join(areas) or '(unresolved)'}\n"
               f"LANGUAGES: {', '.join(languages)}\n")
    parsed = llm.call_json("a06.query_vocabulary", payload, max_tokens=1500, default={}) or {}

    vocab.indication_synonyms = _dedup([indication] + (parsed.get("indication_synonyms") or []))
    vocab.indication_abbreviations = _dedup(parsed.get("indication_abbreviations") or [])
    vocab.disease_class_terms = _dedup(parsed.get("disease_class_terms") or [])
    vocab.outcome_requirement_terms = _dedup(parsed.get("outcome_requirement_terms") or [])
    localised = parsed.get("localised_assessment_terms") or {}
    vocab.localised_assessment_terms = {
        k: _dedup(v) for k, v in localised.items() if isinstance(v, list)}

    # Safety net. If the model named something that looks like a drug, drop it —
    # the vocabulary must not carry an answer.
    vocab = _strip_possible_drug_names(vocab)
    return vocab


_DRUGLIKE = re.compile(r"\b\w+(?:mab|nib|tinib|ciclib|zumab|ximab|umab|parib|"
                       r"platin|rubicin|taxel|mustine|tecan)\b", re.IGNORECASE)


def _strip_possible_drug_names(v: QueryVocabulary) -> QueryVocabulary:
    def clean(items: List[str]) -> List[str]:
        return [i for i in items if not _DRUGLIKE.search(i or "")]
    v.indication_synonyms = clean(v.indication_synonyms)
    v.disease_class_terms = clean(v.disease_class_terms)
    v.outcome_requirement_terms = clean(v.outcome_requirement_terms)
    return v


def _dedup(items: Iterable[str]) -> List[str]:
    out, seen = [], set()
    for i in items or []:
        s = str(i or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def plan_queries(population: Population, intervention: Intervention,
                 areas: Sequence[str], vocab: QueryVocabulary,
                 inventory: SourceInventory) -> List[QueryPlanItem]:
    """Compose the full query plan deterministically.

    Templates, not a model. Reviewable, diffable, testable, free.
    """
    drug = intervention.product_name
    indication = population.value("indication_disease")
    syn = vocab.indication_synonyms or [indication]
    primary = syn[0] if syn else indication
    plan: List[QueryPlanItem] = []

    # -- Tier 1, EU-wide: the regulatory record -----------------------------
    ema_domains = inventory.domains(C.SRC_DRUG_LABEL)
    if ema_domains:
        plan.append(QueryPlanItem(
            query=f"{drug} EPAR summary of product characteristics",
            source_class=C.SRC_DRUG_LABEL, member_state=C.EU_WIDE,
            pass_type=PASS_DRUG_ANCHORED, domains=ema_domains, max_urls=3,
            note="EU-wide regulatory record; read in full."))

    # -- Tier 1, per Member State ------------------------------------------
    for state in C.EU_27_MEMBER_STATES:
        lang = STATE_LANGUAGE.get(state, "en")
        hta_domains = inventory.domains(C.SRC_HTA_REGULATORY, member_state=state,
                                        include_shared=False)
        # THE AREA FILTER. Without it this list is every specialty's societies.
        guide_domains = inventory.domains(C.SRC_CLINICAL_GUIDELINE, member_state=state,
                                          areas=areas)

        if hta_domains:
            terms = vocab.localised_assessment_terms.get(lang) or ["assessment", "appraisal"]
            plan.append(QueryPlanItem(
                query=f"{drug} {primary} {terms[0]}",
                source_class=C.SRC_HTA_REGULATORY, member_state=state,
                pass_type=PASS_DRUG_ANCHORED, domains=hta_domains,
                language=lang, note="National HTA body, drug-anchored."))
        else:
            plan.append(QueryPlanItem(
                query=f"{state} health technology assessment {drug} {primary}",
                source_class=C.SRC_HTA_REGULATORY, member_state=state,
                pass_type=PASS_DRUG_ANCHORED, domains=[], max_urls=2,
                language=lang,
                note=("No curated HTA source for this state; unscoped fallback so the "
                      "state is genuinely searched rather than silently skipped.")))

        if guide_domains:
            plan.append(QueryPlanItem(
                query=f"{drug} {primary}",
                source_class=C.SRC_CLINICAL_GUIDELINE, member_state=state,
                pass_type=PASS_DRUG_ANCHORED, domains=guide_domains, language=lang,
                note="Area-filtered guideline sources, drug-anchored."))
            # The landscape pass. Deliberately omits the drug.
            plan.append(QueryPlanItem(
                query=f"{primary} treatment guideline recommended options "
                      f"{_line_hint(population, intervention)}".strip(),
                source_class=C.SRC_CLINICAL_GUIDELINE, member_state=state,
                pass_type=PASS_LANDSCAPE, domains=guide_domains, language=lang,
                note=("NOT drug-anchored: finds the complete treatment-line landscape a "
                      "guideline states, which a drug-anchored query systematically misses.")))

    # -- Tier 2: conference evidence (no API exists) ------------------------
    conf_domains = inventory.domains(C.SRC_CONFERENCE) or []
    if conf_domains:
        plan.append(QueryPlanItem(
            query=f"{drug} {primary} abstract",
            source_class=C.SRC_CONFERENCE, member_state=C.GENERAL_EVIDENCE,
            pass_type=PASS_DRUG_ANCHORED, domains=conf_domains,
            note="Kept separate from peer-reviewed evidence, per SME Agent 8."))

    # -- Outcome REQUIREMENTS: a different intent from outcome results ------
    req_terms = vocab.outcome_requirement_terms or ["required outcomes", "outcome measures"]
    guide_any = inventory.domains(C.SRC_CLINICAL_GUIDELINE, areas=areas)
    if guide_any:
        plan.append(QueryPlanItem(
            query=f"{primary} {req_terms[0]} health technology assessment",
            source_class=C.SRC_CLINICAL_GUIDELINE, member_state=C.GENERAL_EVIDENCE,
            pass_type=PASS_OUTCOME_REQUIREMENT, domains=guide_any, max_urls=3,
            note=("Outcomes an assessment REQUIRES, which is a different object from "
                  "outcomes a trial happens to report.")))

    # -- Tier 3 -------------------------------------------------------------
    if C.ENABLE_TIER_3_GENERAL_WEB:
        plan.append(QueryPlanItem(
            query=f"{drug} {primary} standard of care comparator",
            source_class=C.SRC_GENERAL_WEB, member_state=C.GENERAL_EVIDENCE,
            pass_type=PASS_DRUG_ANCHORED, domains=[], max_urls=2,
            note="Tier 3. Disabled by default: MAI-34392 excludes non-peer-reviewed web."))

    return plan


def _line_hint(population: Population, intervention: Intervention) -> str:
    """A line-of-therapy hint for the landscape query, only when the user
    actually stated one. Never invented."""
    for value in (intervention.value("line_of_therapy"),
                  population.value("prior_therapy_line")):
        if value:
            return value
    return ""


def plan_refinement(gaps: Sequence[Tuple[str, str]], population: Population,
                    intervention: Intervention, vocab: QueryVocabulary,
                    inventory: SourceInventory,
                    areas: Sequence[str]) -> List[QueryPlanItem]:
    """One bounded refinement round, fired only where coverage actually failed.

    Broadens terms. Never introduces a candidate comparator name.
    """
    if not C.RETRIEVAL.enable_refinement_round:
        return []
    drug = intervention.product_name
    broad = (vocab.disease_class_terms or vocab.indication_synonyms
             or [population.value("indication_disease")])[0]
    out: List[QueryPlanItem] = []
    for state, source_class in gaps:
        domains = (inventory.domains(source_class, member_state=state, areas=areas)
                   if source_class == C.SRC_CLINICAL_GUIDELINE
                   else inventory.domains(source_class, member_state=state))
        out.append(QueryPlanItem(
            query=f"{broad} {drug}" if source_class != C.SRC_CLINICAL_GUIDELINE else broad,
            source_class=source_class, member_state=state,
            pass_type=PASS_REFINEMENT, domains=domains, max_urls=2,
            note="Refinement round: broadened terms after a coverage gap."))
    return out


# ===========================================================================
# A7 — Retrieval
# ===========================================================================

@dataclass
class RetrievalResult:
    documents: List[RetrievedDocument] = field(default_factory=list)
    trials: List[TrialRecord] = field(default_factory=list)
    publications: List[PublicationRecord] = field(default_factory=list)
    attempts: List[SourceClassAttempt] = field(default_factory=list)
    blocked_by_guard: List[str] = field(default_factory=list)


def execute_plan(plan: Sequence[QueryPlanItem], search: SearchProvider,
                 guard: LeakageGuard, inventory: SourceInventory,
                 areas: Sequence[str],
                 max_workers: int = C.RETRIEVAL.max_workers) -> RetrievalResult:
    """Run the plan. One unit of work per plan item; states are independent."""
    result = RetrievalResult()

    # Direct-extract any curated entry that is already a document URL — no
    # search needed, and no ranking luck involved.
    for entry in inventory.document_urls():
        if entry.therapeutic_area and areas and entry.therapeutic_area not in set(areas):
            continue
        if guard.is_blocked(entry.url):
            guard.record(entry.url)
            result.blocked_by_guard.append(entry.url)
            continue
        doc = search.fetch(entry.url, full_document=True)
        doc.source_class = entry.source_class
        doc.member_state = entry.member_state or C.EU_WIDE
        doc.organization = entry.organization
        if doc.ok:
            result.documents.append(doc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_execute_item, item, search, guard): item for item in plan}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                docs, attempt, blocked = fut.result()
            except Exception as exc:  # noqa: BLE001 — one item must not lose the rest
                result.attempts.append(SourceClassAttempt(
                    member_state=item.member_state, source_class=item.source_class,
                    attempted=True, status=C.EV_RETRIEVAL_FAILED,
                    detail=f"{type(exc).__name__}: {exc}"))
                continue
            result.documents.extend(docs)
            result.attempts.append(attempt)
            result.blocked_by_guard.extend(blocked)

    result.documents = _dedupe_documents(result.documents, inventory)
    return result


def _dedupe_documents(documents: List[RetrievedDocument],
                      inventory: SourceInventory) -> List[RetrievedDocument]:
    """One document, one extraction.

    Several plan items legitimately surface the same URL — an unscoped fallback
    for a state with no curated source, a landscape pass and a drug-anchored
    pass over the same guideline domain. Extracting it more than once multiplies
    LLM cost and inflates every downstream count, which is how one document ends
    up looking like corroboration across several Member States.

    Member-State attribution is UNIONED rather than dropped: the same guideline
    really can be the source for several states, and losing that would be worse
    than the duplication.
    """
    # Domain -> the source class the curated list says it is. An unscoped
    # fallback search must not stamp its own class onto a document that the
    # source list identifies as something else.
    class_by_domain: Dict[str, str] = {}
    for e in inventory.entries:
        class_by_domain.setdefault(e.domain, e.source_class)

    merged: Dict[str, RetrievedDocument] = {}
    states: Dict[str, List[str]] = {}
    for doc in documents:
        key = doc.resolved_url or doc.url
        true_class = class_by_domain.get(normalise_domain(key))
        if true_class and true_class != doc.source_class:
            doc.source_class = true_class
        if key not in merged:
            merged[key] = doc
            states[key] = []
        if doc.member_state in C.EU_27_MEMBER_STATES:
            if doc.member_state not in states[key]:
                states[key].append(doc.member_state)

    out = []
    for key, doc in merged.items():
        attributed = states.get(key) or []
        if attributed:
            doc.member_state = attributed[0]
            # Keep the full attribution so consolidation can credit every state
            # this document genuinely supports.
            doc.__dict__["attributed_states"] = attributed
        out.append(doc)
    return out


def _execute_item(item: QueryPlanItem, search: SearchProvider, guard: LeakageGuard
                  ) -> Tuple[List[RetrievedDocument], SourceClassAttempt, List[str]]:
    attempt = SourceClassAttempt(member_state=item.member_state,
                                 source_class=item.source_class, attempted=True)
    blocked: List[str] = []

    if not item.domains and item.source_class in (C.SRC_HTA_REGULATORY,):
        # An unscoped fallback is still a genuine search — recorded as such so
        # "no curated source" is never confused with "not searched".
        attempt.detail = "no curated domains; unscoped fallback search"

    hits: List[SearchHit] = search.search(item.query, item.domains,
                                          C.RETRIEVAL.search_max_results)
    if not hits:
        failed = any(c.error for c in search.call_log[-1:])
        attempt.status = C.EV_RETRIEVAL_FAILED if failed else C.EV_NONE
        return [], attempt, blocked

    urls = select_balanced(hits, item.max_urls, {"primary": item.domains} if item.domains else {})
    docs: List[RetrievedDocument] = []
    for url in urls:
        if guard.is_blocked(url):
            guard.record(url)
            blocked.append(url)
            continue
        full = item.source_class in C.FULL_DOCUMENT_SOURCE_CLASSES
        doc = search.fetch(url, query=item.query, full_document=full)
        doc.source_class = item.source_class
        doc.member_state = item.member_state
        if doc.ok:
            docs.append(doc)

    attempt.documents_retrieved = len(docs)
    attempt.status = C.EV_FOUND if docs else (
        C.EV_SOURCE_INACCESSIBLE if urls else C.EV_NONE)
    return docs, attempt, blocked


def retrieve_structured(population: Population, intervention: Intervention,
                        indication: LicensedIndicationRecord,
                        trials: Optional[TrialRegistryClient],
                        literature: Optional[LiteratureClient],
                        vocab: QueryVocabulary) -> RetrievalResult:
    """Structured sources. These bypass LLM extraction for the fields the API
    already types."""
    out = RetrievalResult()
    condition = population.value("indication_disease")
    drug = intervention.product_name

    if trials is not None:
        try:
            out.trials = trials.search(condition=condition, intervention=drug,
                                       identifiers=indication.pivotal_trials,
                                       max_results=20)
            out.attempts.append(SourceClassAttempt(
                member_state=C.GENERAL_EVIDENCE, source_class=C.SRC_TRIAL_REGISTRY,
                attempted=True, documents_retrieved=len(out.trials),
                status=C.EV_FOUND if out.trials else C.EV_NONE))
        except Exception as exc:  # noqa: BLE001
            out.attempts.append(SourceClassAttempt(
                member_state=C.GENERAL_EVIDENCE, source_class=C.SRC_TRIAL_REGISTRY,
                attempted=True, status=C.EV_RETRIEVAL_FAILED,
                detail=f"{type(exc).__name__}: {exc}"))

    if literature is not None:
        try:
            broad = (vocab.disease_class_terms or vocab.indication_synonyms or [condition])[0]
            pubs = literature.search(f"{drug} {condition}", max_results=15)
            # Guidelines as a retrievable CLASS — the query shape a web search
            # cannot express, and the one that finds a landscape guideline.
            pubs += literature.search(broad,
                                      publication_types=["Practice Guideline", "Guideline"],
                                      max_results=15)
            seen, deduped = set(), []
            for p in pubs:
                if p.pmid and p.pmid not in seen:
                    seen.add(p.pmid)
                    deduped.append(p)
            out.publications = deduped
            out.attempts.append(SourceClassAttempt(
                member_state=C.GENERAL_EVIDENCE, source_class=C.SRC_PUBMED,
                attempted=True, documents_retrieved=len(deduped),
                status=C.EV_FOUND if deduped else C.EV_NONE))
        except Exception as exc:  # noqa: BLE001
            out.attempts.append(SourceClassAttempt(
                member_state=C.GENERAL_EVIDENCE, source_class=C.SRC_PUBMED,
                attempted=True, status=C.EV_RETRIEVAL_FAILED,
                detail=f"{type(exc).__name__}: {exc}"))
    return out


# ===========================================================================
# A8 — Evidence extraction
# ===========================================================================

_seq = {"n": 0}


def _next_id(prefix: str) -> str:
    _seq["n"] += 1
    return f"{prefix}-{_seq['n']:05d}"


def extract_from_documents(documents: Sequence[RetrievedDocument],
                           population: Population, intervention: Intervention,
                           llm: LLMClient,
                           max_workers: int = C.RETRIEVAL.max_workers
                           ) -> List[EvidenceRecord]:
    """One typed contract for every source class.

    The SME writes seven retrieval agents, but they record the same finding
    shape. Seven prompts would be seven places for one rule to drift.
    """
    records: List[EvidenceRecord] = []
    if not documents:
        return records
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_extract_one, d, population, intervention, llm)
                   for d in documents]
        for fut in as_completed(futures):
            try:
                records.extend(fut.result())
            except Exception:  # noqa: BLE001 — one document must not lose the rest
                continue
    return records


def _extract_one(doc: RetrievedDocument, population: Population,
                 intervention: Intervention, llm: LLMClient) -> List[EvidenceRecord]:
    payload = (
        f"REQUESTED INTERVENTION: {intervention.product_name}\n"
        f"REQUESTED POPULATION: {population.value('indication_disease')}\n"
        f"SOURCE CLASS: {doc.source_class}\n"
        f"SOURCE URL: {doc.resolved_url or doc.url}\n\n"
        f"DOCUMENT TEXT:\n{doc.text[:120000]}")
    parsed = llm.call_json("a08.extraction", payload,
                           max_tokens=C.LLM.extraction_max_tokens, default=[])
    if not isinstance(parsed, list):
        return []

    out: List[EvidenceRecord] = []
    tier = C.TIER_OF_SOURCE_CLASS.get(doc.source_class, 3)
    attributed = list(getattr(doc, "attributed_states", None)
                      or doc.__dict__.get("attributed_states") or [])
    for item in parsed:
        if not isinstance(item, dict):
            continue
        ftype = item.get("finding_type")
        if ftype not in (FINDING_COMPARATOR, FINDING_OUTCOME):
            continue

        # The source's own stated Member State wins; otherwise the document's.
        state = C.canonicalize_member_state(item.get("member_state", "")) or doc.member_state
        pc = item.get("population_context") or {}
        rec = EvidenceRecord(
            finding_id=_next_id("cmp" if ftype == FINDING_COMPARATOR else "out"),
            finding_type=ftype,
            subject_drug=(item.get("subject_drug") or "").strip(),
            source_id=doc.source_id,
            source_url=doc.resolved_url or doc.url,
            source_class=doc.source_class,
            tier=tier,
            member_state=state,
            organization=doc.organization,
            document_title=doc.title,
            document_date=doc.published_date,
            language=doc.language,
            retrieval_method=doc.method,
            population_context=PopulationContext(
                disease=pc.get("disease", ""),
                subtype_histology=pc.get("subtype_histology", ""),
                stage=pc.get("stage", ""),
                biomarker=pc.get("biomarker", ""),
                line_of_therapy=pc.get("line_of_therapy", ""),
                prior_therapy=pc.get("prior_therapy", ""),
                treatment_setting_intent=pc.get("treatment_setting_intent", ""),
                age_band=pc.get("age_band", ""),
                other=pc.get("other", ""),
                verbatim=pc.get("verbatim", "")),
            recommendation_strength=item.get("recommendation_strength") or C.REC_NOT_STATED,
            evidence_quote=item.get("evidence_quote", "") or "",
            evidence_locator=item.get("evidence_locator", "") or "",
            general_evidence_flag=(tier in (2, 3)
                                   and state in (C.EU_WIDE, C.GENERAL_EVIDENCE)))
        if doc.source_class == C.SRC_CONFERENCE:
            rec.evidence_status = "conference_abstract"

        if ftype == FINDING_COMPARATOR:
            cmp_block = item.get("comparator") or {}
            as_stated = (cmp_block.get("as_stated") or "").strip()
            if not as_stated:
                continue
            rec.comparator = Comparator(
                as_stated=as_stated,
                role=cmp_block.get("role") or C.ROLE_UNCLEAR,
                is_combination=bool(cmp_block.get("is_combination")),
                components=[c for c in (cmp_block.get("components") or []) if c],
                comparator_scenario=cmp_block.get("comparator_scenario") or "",
                retain_all_status=cmp_block.get("retain_all_status") or C.RETAIN_UNCONFIRMED)
            # INVARIANT 1, enforced at the boundary: extraction never sets a
            # comparator class, whatever the model returned.
            rec.comparator.class_mechanism = ""
            rec.comparator.class_source = ""
        else:
            ob = item.get("outcome") or {}
            measure = (ob.get("measure") or "").strip()
            if not measure:
                continue
            rec.outcome = OutcomeMention(
                measure=measure,
                result=ob.get("result", "") or "",
                unit=ob.get("unit", "") or "",
                instrument=ob.get("instrument", "") or "",
                requirement_type=ob.get("requirement_type", "") or "",
                is_requirement=bool(ob.get("is_requirement")))
        out.append(rec)

    # One document can legitimately be the curated source for several Member
    # States (a shared pan-European guideline is the common case). When the
    # SOURCE itself names no state, credit every state whose curated list points
    # at this document — but only for comparators, since outcomes are one
    # EU-wide list and per-state duplication would be meaningless there.
    if len(attributed) > 1:
        extra: List[EvidenceRecord] = []
        for rec in out:
            if rec.finding_type != FINDING_COMPARATOR:
                continue
            if (item_state := rec.member_state) not in attributed:
                continue
            for state in attributed:
                if state == item_state:
                    continue
                clone = EvidenceRecord(**{**rec.__dict__,
                                          "finding_id": _next_id("cmp"),
                                          "member_state": state})
                extra.append(clone)
        out.extend(extra)
    return out


def records_from_trials(trials: Sequence[TrialRecord], intervention: Intervention
                        ) -> List[EvidenceRecord]:
    """Typed trial arms straight into evidence records — no LLM.

    `armGroups[].interventions[]` already answers "what was this compared
    against". Paying a model to re-derive it from HTML is the most expensive
    possible way to read a database column.
    """
    drug = intervention.product_name
    out: List[EvidenceRecord] = []
    for trial in trials:
        for arm in trial.comparator_arms(drug):
            name = ", ".join(arm.interventions) or arm.label
            if not name:
                continue
            is_combo = len(arm.interventions) > 1
            rec = EvidenceRecord(
                finding_id=_next_id("cmp"),
                finding_type=FINDING_COMPARATOR,
                subject_drug=drug,
                source_id="trial-" + trial.identifier,
                source_url=trial.url,
                source_class=C.SRC_TRIAL_REGISTRY,
                tier=C.TIER_OF_SOURCE_CLASS[C.SRC_TRIAL_REGISTRY],
                member_state=C.GENERAL_EVIDENCE,
                organization=trial.registry,
                document_title=trial.title,
                retrieval_method="registry_api",
                general_evidence_flag=True,
                comparator=Comparator(
                    as_stated=name,
                    role=(C.ROLE_ACTIVE_COMPARATOR
                          if "COMPARATOR" in (arm.arm_type or "").upper()
                          else C.ROLE_UNCLEAR),
                    is_combination=is_combo,
                    components=list(arm.interventions)),
                population_context=PopulationContext(
                    disease=", ".join(trial.conditions),
                    verbatim=(arm.description or "")[:500]),
                evidence_quote=(f"Arm '{arm.label}' ({arm.arm_type}): "
                                f"{', '.join(arm.interventions)}. {arm.description}").strip()[:1000],
                evidence_locator=f"{trial.identifier} armGroups",
                # Structured API fields are self-grounding: the value IS the
                # field, not an inference from prose.
                grounded=True)
            out.append(rec)

        for measure in list(trial.primary_outcomes) + list(trial.secondary_outcomes):
            if not measure:
                continue
            out.append(EvidenceRecord(
                finding_id=_next_id("out"),
                finding_type=FINDING_OUTCOME,
                subject_drug=drug,
                source_id="trial-" + trial.identifier,
                source_url=trial.url,
                source_class=C.SRC_TRIAL_REGISTRY,
                tier=C.TIER_OF_SOURCE_CLASS[C.SRC_TRIAL_REGISTRY],
                member_state=C.GENERAL_EVIDENCE,
                document_title=trial.title,
                retrieval_method="registry_api",
                general_evidence_flag=True,
                outcome=OutcomeMention(measure=measure, is_requirement=False),
                population_context=PopulationContext(disease=", ".join(trial.conditions)),
                evidence_quote=f"{trial.identifier} outcome measure: {measure}",
                evidence_locator=f"{trial.identifier} outcomesModule",
                grounded=True))
    return out
