"""
A9   Grounding             — does the value trace to the retrieved text?
A10  Claim Validation      — does the RE-FETCHED source support the claim here?
A11  Comparator Identity   — substance identity, and the comparator's OWN class
A12  Scope Adjudication    — separated from candidate generation, on purpose
A13  Per-State Assignment  — a verdict for all 27, not just states with findings

A9 and A10 are separate because they fail differently. Grounding catches
fabrication; claim validation catches "right drug, wrong line of therapy". The
legacy implementation fused them and did neither: it compared a value against a
±140-character window of already-fetched text and called that validation.
"""

from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config as C
from ..providers.llm import LLMClient
from ..providers.search import SearchProvider
from ..schema import (Comparator, EvidenceCitation, EvidenceRecord, FINDING_COMPARATOR,
                      Intervention, MemberStateVerdict, RetrievedDocument,
                      ScopeAdjudication, ScopeBoundary, ValidationOutcome)

# ===========================================================================
# A9 — Grounding
# ===========================================================================

GROUNDING_OVERLAP_THRESHOLD = 0.62
_WORD = re.compile(r"[a-z0-9]+")
_NUM = re.compile(r"\d[\d.,]*")


def _normalise(text: str) -> str:
    """NFKC so a ligature in a PDF ('eﬀective') matches its typed form."""
    return unicodedata.normalize("NFKC", text or "").lower()


def _tokens(text: str) -> List[str]:
    return [w for w in _WORD.findall(_normalise(text)) if len(w) > 2]


def is_grounded(value: str, document_text: str) -> Tuple[bool, str]:
    """Exact substring, else a word-overlap floor.

    Deliberately a cheap check. Its job is to catch invention, not to judge
    meaning — that is A10's job, and conflating the two is what made the legacy
    validator both permissive and blind.
    """
    if not value:
        return False, "empty value"
    doc = _normalise(document_text)
    if not doc:
        return False, "no document text"
    if _normalise(value) in doc:
        return True, "exact"
    vt = _tokens(value)
    if not vt:
        return False, "no comparable tokens"
    present = sum(1 for t in vt if t in doc)
    ratio = present / len(vt)
    if ratio >= GROUNDING_OVERLAP_THRESHOLD:
        return True, f"overlap {ratio:.2f}"
    return False, f"overlap {ratio:.2f} below {GROUNDING_OVERLAP_THRESHOLD}"


def best_quote_window(value: str, document_text: str, pad: int = 200) -> str:
    """Pick the densest window around the value.

    Anchors on NUMERALS first and weights them, because a quote that shares no
    distinctive token with the value is worse than no quote — it looks like
    traceability and is not.
    """
    doc = document_text or ""
    low = _normalise(doc)
    anchors: List[int] = []
    for num in _NUM.findall(value or "")[:4]:
        idx = low.find(num.lower())
        if idx >= 0:
            anchors.append(idx)
    for tok in _tokens(value)[:8]:
        idx = low.find(tok)
        if idx >= 0:
            anchors.append(idx)
    if not anchors:
        return ""
    value_tokens = set(_tokens(value))
    value_nums = set(n.lower() for n in _NUM.findall(value or ""))
    best, best_score = "", 0.0
    for a in anchors:
        start, end = max(0, a - pad), min(len(doc), a + pad)
        window = doc[start:end]
        wl = _normalise(window)
        score = (sum(3 for n in value_nums if n in wl)
                 + sum(1 for t in value_tokens if t in wl))
        if score > best_score:
            best, best_score = window.strip(), score
    return best if best_score > 0 else ""


def ground_records(records: Sequence[EvidenceRecord],
                   documents: Sequence[RetrievedDocument]) -> List[EvidenceRecord]:
    """Ground each record against the document it came from."""
    by_id = {d.source_id: d for d in documents}
    for rec in records:
        if rec.grounded:
            continue   # structured API records are self-grounding
        doc = by_id.get(rec.source_id)
        if doc is None or not doc.text:
            rec.grounded = False
            rec.grounding_note = "source document not available for grounding"
            continue
        value = (rec.comparator.as_stated if rec.comparator
                 else (rec.outcome.measure if rec.outcome else ""))
        ok_value, note_value = is_grounded(value, doc.text)
        ok_quote, note_quote = (is_grounded(rec.evidence_quote, doc.text)
                                if rec.evidence_quote else (False, "no quote"))
        rec.grounded = ok_value and ok_quote
        rec.grounding_note = f"value: {note_value}; quote: {note_quote}"
        if not rec.evidence_quote or not ok_quote:
            better = best_quote_window(value, doc.text)
            if better:
                rec.evidence_quote = better[:1200]
                rec.grounded = ok_value
                rec.grounding_note = f"value: {note_value}; quote: recovered from source"
    return list(records)


# ===========================================================================
# A10 — Claim validation (with a real re-fetch)
# ===========================================================================

def validate_claims(records: Sequence[EvidenceRecord], population_indication: str,
                    intervention: Intervention, search: SearchProvider,
                    llm: LLMClient, max_workers: int = C.RETRIEVAL.max_workers
                    ) -> List[EvidenceRecord]:
    """Re-open each cited source and confirm the exact claim, in context.

    The SME is explicit: "Open the cited source directly — do not evaluate a
    finding based on its stated citation alone." Batched per source, which the
    SME permits, with the same standard applied regardless of batch size.
    """
    drug = intervention.product_name

    # Deterministic pre-filter: subject-drug and role. Both are hard rules that
    # do not need a model, and both are cheaper caught here.
    survivors: List[EvidenceRecord] = []
    for rec in records:
        if not rec.grounded:
            rec.validation = ValidationOutcome(
                verdict=C.V_NOT_SUPPORTED,
                reason="not grounded in the retrieved source text")
            continue
        if rec.retrieval_method == "registry_api":
            # A structured API record is self-evidencing for the fields the API
            # types: the comparator IS `armGroups[].interventions[]`, not an
            # inference from prose, so there is no citation to re-open and
            # nothing a re-read could contradict. It still goes through scope
            # adjudication (A12), which is where a registry arm from the wrong
            # population gets caught.
            rec.validation = ValidationOutcome(
                verdict=C.V_SUPPORTED, refetched=False, attempts=0,
                reason=("structured registry field; self-evidencing, so no document "
                        "re-fetch applies. Scope relevance is still adjudicated."))
            continue
        if rec.comparator is not None:
            if _is_other_drug(rec.subject_drug, drug):
                rec.validation = ValidationOutcome(
                    verdict=C.V_WRONG_SUBJECT_DRUG,
                    reason=(f"claim is about {rec.subject_drug!r}, not the requested "
                            f"intervention {drug!r}"))
                continue
            if rec.comparator.role in (C.ROLE_INTERVENTION_ARM,):
                rec.validation = ValidationOutcome(
                    verdict=C.V_WRONG_INTERVENTION,
                    reason="this is the intervention's own arm, not a comparator")
                continue
            if _same_substance(rec.comparator.as_stated, drug):
                rec.validation = ValidationOutcome(
                    verdict=C.V_WRONG_INTERVENTION,
                    reason="a comparator cannot be the intervention being assessed")
                continue
        survivors.append(rec)

    by_source: Dict[str, List[EvidenceRecord]] = {}
    for rec in survivors:
        by_source.setdefault(rec.source_url, []).append(rec)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_validate_batch, url, batch, population_indication,
                               drug, search, llm)
                   for url, batch in by_source.items()]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:  # noqa: BLE001 — a batch failure must fail closed,
                continue        # which _validate_batch already does per record
    return list(records)


def _validate_batch(url: str, batch: List[EvidenceRecord], indication: str,
                    drug: str, search: SearchProvider, llm: LLMClient) -> None:
    doc = search.fetch(url, full_document=True, use_cache=False)  # genuine re-fetch
    if not doc.ok or not doc.text:
        for rec in batch:
            rec.validation = ValidationOutcome(
                verdict=C.V_SOURCE_INACCESSIBLE, refetched=True, attempts=1,
                reason=f"source could not be re-opened: {doc.error or doc.status}")
        return

    lines = []
    for i, rec in enumerate(batch, start=1):
        if rec.comparator:
            claim = (f"COMPARATOR {rec.comparator.as_stated!r} "
                     f"(role: {rec.comparator.role}) for subject drug "
                     f"{rec.subject_drug or drug!r}")
        else:
            o = rec.outcome
            claim = (f"OUTCOME {o.measure!r}"
                     + (f" = {o.result!r}" if o and o.result else "")
                     + (" [stated as a REQUIRED scope outcome]" if o and o.is_requirement else ""))
        lines.append(f"{i}. {claim}\n   population context stated by the source: "
                     f"{rec.population_context.summary() or '(none stated)'}\n"
                     f"   quoted evidence: {rec.evidence_quote[:400]}")

    payload = (f"REQUESTED POPULATION: {indication}\n"
               f"REQUESTED INTERVENTION: {drug}\n\n"
               f"CLAIMS TO VALIDATE:\n" + "\n".join(lines)
               + f"\n\nRE-FETCHED SOURCE DOCUMENT ({url}):\n{doc.text[:100000]}")

    parsed = llm.call_json("a10.claim_validation", payload,
                           max_tokens=C.LLM.validation_max_tokens, default=None)
    results = (parsed or {}).get("results") if isinstance(parsed, dict) else None
    if not results:
        # Fail CLOSED. An unparseable response must never pass a finding
        # through unchecked.
        for rec in batch:
            rec.validation = ValidationOutcome(
                verdict=C.V_NOT_SUPPORTED, refetched=True, attempts=1,
                reason="validator response could not be parsed; failed closed")
        return

    seen = set()
    for item in results:
        idx = item.get("index")
        if not isinstance(idx, int) or not (1 <= idx <= len(batch)):
            continue
        seen.add(idx)
        rec = batch[idx - 1]
        verdict = item.get("verdict", C.V_NOT_SUPPORTED)
        rec.validation = ValidationOutcome(
            verdict=verdict, reason=item.get("reason", ""), refetched=True, attempts=1)
    for i, rec in enumerate(batch, start=1):
        if i not in seen:
            rec.validation = ValidationOutcome(
                verdict=C.V_NOT_SUPPORTED, refetched=True, attempts=1,
                reason="validator did not return a verdict for this claim; failed closed")


_STOP = {"plus", "and", "with", "alone", "monotherapy", "therapy", "treatment",
         "regimen", "combination", "based", "chemotherapy"}


def _substance_tokens(text: str) -> set:
    return {t for t in _WORD.findall(_normalise(text))
            if len(t) > 3 and t not in _STOP}


def _same_substance(a: str, b: str) -> bool:
    ta, tb = _substance_tokens(a), _substance_tokens(b)
    return bool(ta) and ta == tb


def _is_other_drug(subject: str, requested: str) -> bool:
    """True when the record's subject drug is clearly a DIFFERENT drug.

    Silence is not a mismatch — an empty subject_drug means the extractor could
    not tell, which is handled by the LLM validator with the document in hand.
    """
    if not subject:
        return False
    ts, tr = _substance_tokens(subject), _substance_tokens(requested)
    if not ts or not tr:
        return False
    return not (ts & tr)


# ===========================================================================
# A11 — Comparator identity
# ===========================================================================

def _load_vocab() -> Dict[str, Any]:
    import os
    path = os.path.join(C.DATA_DIR, "inn_atc_seed.json")
    if not os.path.exists(path):
        return {"substances": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_VOCAB = _load_vocab()
_BY_ALIAS: Dict[str, Dict[str, Any]] = {}
for _s in _VOCAB.get("substances", []):
    for _alias in [_s["inn"]] + _s.get("brand_names", []) + _s.get("aliases", []):
        _BY_ALIAS[_normalise(_alias)] = _s


def resolve_identities(records: Sequence[EvidenceRecord], llm: Optional[LLMClient],
                       batch_size: int = 40) -> Dict[str, Comparator]:
    """Resolve every distinct comparator string to a substance identity.

    Vocabulary lookup FIRST. A curated INN/ATC table is deterministic, free and
    auditable; the model is for the residue only. The legacy implementation had
    a five-entry hardcoded brand table and asked an LLM for everything else,
    including the class — which is how a comparator inherited the intervention's
    mechanism.
    """
    distinct: List[str] = []
    seen = set()
    for rec in records:
        if rec.comparator and rec.comparator.as_stated:
            key = _normalise(rec.comparator.as_stated)
            if key not in seen:
                seen.add(key)
                distinct.append(rec.comparator.as_stated)

    resolved: Dict[str, Comparator] = {}
    unresolved: List[str] = []

    for name in distinct:
        entry = _BY_ALIAS.get(_normalise(name))
        if entry:
            resolved[_normalise(name)] = Comparator(
                as_stated=name, inn=entry["inn"], atc_code=entry.get("atc", ""),
                class_mechanism=entry.get("class_mechanism", ""),
                class_source="atc_vocabulary",
                is_combination=False, components=[entry["inn"]])
        else:
            unresolved.append(name)

    if unresolved and llm is not None:
        for start in range(0, len(unresolved), batch_size):
            batch = unresolved[start:start + batch_size]
            payload = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(batch))
            parsed = llm.call_json("a11.comparator_identity", payload,
                                   max_tokens=4000, default=None)
            if not parsed:
                continue
            for item in parsed.get("items", []) or []:
                idx = item.get("index")
                if not isinstance(idx, int) or not (1 <= idx <= len(batch)):
                    continue
                name = batch[idx - 1]
                inn = (item.get("inn") or "").strip()
                display = (item.get("display_name") or inn or name).strip()
                is_cat = bool(item.get("is_category"))
                comp = Comparator(
                    as_stated=name,
                    inn=inn if not is_cat else "",
                    atc_code=item.get("atc_code", "") or "",
                    class_mechanism=(item.get("class_mechanism") or "").strip(),
                    class_source="source_stated" if item.get("class_mechanism") else "unresolved",
                    is_combination=bool(item.get("is_combination")),
                    components=[c for c in (item.get("components") or []) if c])
                comp.as_stated = display or name
                resolved[_normalise(name)] = comp
            for item in parsed.get("excluded", []) or []:
                idx = item.get("index")
                if isinstance(idx, int) and 1 <= idx <= len(batch):
                    resolved.pop(_normalise(batch[idx - 1]), None)

    # Anything still unresolved keeps its own wording. Under-resolution is
    # recoverable; inventing an identity is not.
    for name in distinct:
        resolved.setdefault(_normalise(name), Comparator(
            as_stated=name, inn="", class_mechanism="", class_source="unresolved"))
    return resolved


def apply_identities(records: Sequence[EvidenceRecord],
                     identities: Dict[str, Comparator]) -> None:
    for rec in records:
        if not rec.comparator:
            continue
        ident = identities.get(_normalise(rec.comparator.as_stated))
        if not ident:
            continue
        rec.comparator.inn = ident.inn
        rec.comparator.atc_code = ident.atc_code
        rec.comparator.class_mechanism = ident.class_mechanism
        rec.comparator.class_source = ident.class_source
        rec.comparator.is_combination = ident.is_combination or rec.comparator.is_combination
        if ident.components:
            rec.comparator.components = ident.components


def identity_key(comp: Comparator) -> str:
    """Merge key. INN when known; otherwise the normalised stated wording.

    Combinations merge on their COMPLETE component set — two regimens sharing
    one ingredient are two regimens, never a third synthesised one.
    """
    if comp.is_combination and comp.components:
        return "+".join(sorted(_normalise(c) for c in comp.components))
    if comp.inn:
        return _normalise(comp.inn)
    return _normalise(comp.as_stated)


# ===========================================================================
# A12 — Scope adjudication
# ===========================================================================

def adjudicate_scope(candidate_key: str, comp: Comparator,
                     records: Sequence[EvidenceRecord], boundary: ScopeBoundary,
                     intervention: Intervention, llm: Optional[LLMClient]
                     ) -> ScopeAdjudication:
    """Is this candidate genuinely in scope for THIS population?

    Retrieval is deliberately broad. This is the step that makes it precise —
    and separating the two is what lets recall and precision be tuned
    independently. Both supporting and opposing evidence are retained, so an
    exclusion is reviewable rather than a silent drop.
    """
    version = "a12.scope_adjudication/v1"

    # Deterministic rejection: nothing here is an active comparator.
    roles = {r.comparator.role for r in records if r.comparator}
    if roles and roles.isdisjoint({C.ROLE_ACTIVE_COMPARATOR, C.ROLE_UNCLEAR}):
        role = sorted(roles)[0]
        return ScopeAdjudication(
            comparator_inn=comp.inn or comp.as_stated, verdict=C.SCOPE_OUT,
            decisive_facet="role", adjudicator_version=version,
            reason=(f"every source describes this as {role.replace('_', ' ')}, "
                    f"not as a comparator for the requested population"),
            evidence_against=[EvidenceCitation(
                source_id=r.source_id, source_url=r.source_url,
                quote=r.evidence_quote[:400], member_state=r.member_state, tier=r.tier,
                facet_conflict="role",
                detail=f"role={r.comparator.role}") for r in records if r.comparator][:5])

    if llm is None:
        # Without an adjudicator, be honest rather than permissive.
        return ScopeAdjudication(
            comparator_inn=comp.inn or comp.as_stated, verdict=C.SCOPE_UNCERTAIN,
            reason="no adjudicator available; surfaced for reviewer judgment",
            adjudicator_version=version,
            evidence_for=[_cite(r) for r in records[:5]])

    lines = []
    for r in records[:20]:
        lines.append(
            f"- source_id: {r.source_id} | tier {r.tier} | {r.source_class} | "
            f"{r.member_state}\n"
            f"  population stated by this source: "
            f"{r.population_context.summary() or '(none stated)'}\n"
            f"  role: {r.comparator.role if r.comparator else 'n/a'}\n"
            f"  quote: {r.evidence_quote[:300]}")

    payload = (
        f"REQUESTED INTERVENTION: {intervention.product_name}\n\n"
        f"REQUESTED POPULATION FACETS:\n{boundary.describe()}\n\n"
        f"LICENSED / CLAIMED INDICATION WORDING: "
        f"{boundary.licensed_indication_wording or '(not established)'}\n\n"
        f"CANDIDATE COMPARATOR: {comp.as_stated}"
        f"{f' (INN {comp.inn})' if comp.inn else ''}\n\n"
        f"EVIDENCE RETRIEVED FOR THIS CANDIDATE:\n" + "\n".join(lines))

    parsed = llm.call_json("a12.scope_adjudication", payload, max_tokens=2000,
                           default=None)
    if not parsed:
        return ScopeAdjudication(
            comparator_inn=comp.inn or comp.as_stated, verdict=C.SCOPE_UNCERTAIN,
            reason="adjudicator response could not be parsed; surfaced rather than dropped",
            adjudicator_version=version,
            evidence_for=[_cite(r) for r in records[:5]])

    verdict = parsed.get("verdict", C.SCOPE_UNCERTAIN)
    if verdict not in (C.SCOPE_IN, C.SCOPE_OUT, C.SCOPE_UNCERTAIN):
        verdict = C.SCOPE_UNCERTAIN
    by_id = {r.source_id: r for r in records}

    def _to_citations(items, conflict: bool) -> List[EvidenceCitation]:
        out = []
        for it in items or []:
            rec = by_id.get(it.get("source_id", ""))
            out.append(EvidenceCitation(
                source_id=it.get("source_id", ""),
                source_url=rec.source_url if rec else "",
                quote=(rec.evidence_quote[:400] if rec else ""),
                member_state=rec.member_state if rec else "",
                tier=rec.tier if rec else 3,
                facets_matched=list(it.get("facets_matched") or []),
                facet_conflict=it.get("facet_conflict", "") if conflict else "",
                detail=it.get("detail", "")))
        return out

    return ScopeAdjudication(
        comparator_inn=comp.inn or comp.as_stated,
        verdict=verdict,
        decisive_facet=parsed.get("decisive_facet", ""),
        reason=parsed.get("reason", ""),
        evidence_for=_to_citations(parsed.get("evidence_for"), False),
        evidence_against=_to_citations(parsed.get("evidence_against"), True),
        adjudicator_version=version)


def _cite(r: EvidenceRecord) -> EvidenceCitation:
    return EvidenceCitation(source_id=r.source_id, source_url=r.source_url,
                            quote=r.evidence_quote[:400], member_state=r.member_state,
                            tier=r.tier)


# ===========================================================================
# A13 — Per-state assignment
# ===========================================================================

def assign_member_states(records: Sequence[EvidenceRecord],
                         searched_states: Iterable[str]) -> List[MemberStateVerdict]:
    """Produce a verdict for ALL 27 states, not only those with findings.

    The UI renders every state with a two-value legend, so "absent" must never
    be ambiguous. We assert `standard_of_care` only where a state-attributed
    source says so; everywhere else we say `not_established` and why. We do NOT
    assert `not_used`, because nothing in the evidence supports that claim —
    saying "this is not standard of care in Malta" requires a source that says
    so, and the pipeline does not have one.
    """
    searched = set(searched_states)
    by_state: Dict[str, List[EvidenceRecord]] = {}
    for rec in records:
        if rec.member_state in C.EU_27_MEMBER_STATES:
            by_state.setdefault(rec.member_state, []).append(rec)

    verdicts: List[MemberStateVerdict] = []
    for state in C.EU_27_MEMBER_STATES:
        hits = by_state.get(state, [])
        if hits:
            best = sorted(hits, key=lambda r: (r.tier, -len(r.evidence_quote)))[0]
            verdicts.append(MemberStateVerdict(
                member_state=state, verdict=C.STATE_STANDARD_OF_CARE,
                tier=best.tier, source_id=best.source_id, source_url=best.source_url,
                evidence_quote=best.evidence_quote[:400],
                reason="named by a source attributed to this Member State"))
        elif state in searched:
            verdicts.append(MemberStateVerdict(
                member_state=state, verdict=C.STATE_NOT_ESTABLISHED,
                reason=("this state's sources were searched and returned no "
                        "state-specific evidence naming this comparator")))
        else:
            verdicts.append(MemberStateVerdict(
                member_state=state, verdict=C.STATE_NOT_ESTABLISHED,
                reason="no curated source for this state and no fallback result"))
    return verdicts
