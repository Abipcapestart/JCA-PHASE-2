"""
Tests for JCA Phase 1.

Every test here maps to a defect that was actually observed in the legacy
implementation or to an invariant the architecture claims to hold. They run with
no credentials and no network.

Run:  python -m pytest tests/ -v      (or: python tests/test_phase1.py)
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jca_phase1 import config as C
from jca_phase1.agents import a01_a05_input_context as inputs
from jca_phase1.agents import a06_a08_retrieval as retrieval
from jca_phase1.agents import a09_a13_validation as validation
from jca_phase1.agents import a14_a18_consolidation as consolidation
from jca_phase1.providers.llm import ScriptedLLM
from jca_phase1.providers.registries import FixtureTrialRegistry, TrialArm, TrialRecord
from jca_phase1.providers.search import FixtureSearchProvider, SearchHit, select_balanced
from jca_phase1.schema import (Comparator, ConsolidatedComparator, EvidenceRecord,
                               EvidenceRef, Field, FINDING_COMPARATOR, Intervention,
                               LicensedIndicationRecord, MemberStateSummary,
                               ORIGIN_AGENT, Population, PopulationContext,
                               ScopeAdjudication, ValidationOutcome)
from jca_phase1.sources.workbook import (is_document_url, load_source_inventory,
                                         normalise_domain)

FIXTURE_WORKBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "fixtures", "MadeAi_JCA_Source_List.xlsx")


# ===========================================================================
# Source-list loading — the defect that blocked everything else
# ===========================================================================

class TestSourceWorkbook(unittest.TestCase):
    """The legacy loaders read the wrong file, matched sheet names exactly, and
    iterated with values_only=True so hyperlink URLs were invisible."""

    @classmethod
    def setUpClass(cls):
        cls.inv = load_source_inventory(FIXTURE_WORKBOOK, strict=False)

    def test_all_three_data_team_sheets_are_matched(self):
        for role in ("soc", "hta", "emea"):
            self.assertIn(role, self.inv.sheets_matched,
                          f"role {role} was not matched; sheets seen: {self.inv.sheets_seen}")

    def test_sheet_names_match_despite_whitespace_and_plurals(self):
        # 'Standard of Care  Guidelines' (two spaces) vs 'SoC';
        # 'HTA licensing sources' (plural) vs 'HTA licensing source'.
        self.assertEqual(self.inv.sheets_matched["soc"], "Standard of Care  Guidelines")
        self.assertEqual(self.inv.sheets_matched["hta"], "HTA licensing sources")
        self.assertEqual(self.inv.sheets_matched["emea"], "EMEA licensing sources")

    def test_hyperlink_only_urls_are_read(self):
        """Every Standard-of-Care URL is a cell hyperlink with no plain text.
        Reading with values_only=True returns zero, which is the legacy bug."""
        soc = [e for e in self.inv.entries if e.source_class == C.SRC_CLINICAL_GUIDELINE]
        self.assertGreater(len(soc), 200,
                           "hyperlink-only SoC URLs were not extracted")

    def test_all_three_sheets_contribute_entries(self):
        classes = {e.source_class for e in self.inv.entries}
        self.assertIn(C.SRC_CLINICAL_GUIDELINE, classes)
        self.assertIn(C.SRC_HTA_REGULATORY, classes)
        self.assertIn(C.SRC_DRUG_LABEL, classes)

    def test_no_load_errors(self):
        errors = [p for p in self.inv.problems if p.severity == "ERROR"]
        self.assertEqual(errors, [], f"source list reported errors: {errors}")

    def test_therapeutic_area_filter_narrows_the_search_surface(self):
        """The single largest measured defect: without the area filter ~89% of
        the domains offered per state are the wrong specialty."""
        all_areas = self.inv.domains(C.SRC_CLINICAL_GUIDELINE, member_state="Austria")
        oncology = self.inv.domains(C.SRC_CLINICAL_GUIDELINE, member_state="Austria",
                                    areas=["Oncology"])
        self.assertGreater(len(all_areas), len(oncology))
        self.assertLessEqual(len(oncology), 3)

    def test_missing_workbook_fails_loudly(self):
        with self.assertRaises(FileNotFoundError):
            load_source_inventory("/nonexistent/workbook.xlsx", strict=True)

    def test_normalise_domain_accepts_bare_domains_and_urls(self):
        # A bare domain returning "" silently empties every domain-scoped search.
        self.assertEqual(normalise_domain("ema.europa.eu"), "ema.europa.eu")
        self.assertEqual(normalise_domain("www.ema.europa.eu"), "ema.europa.eu")
        self.assertEqual(normalise_domain("https://www.ema.europa.eu/en/x"), "ema.europa.eu")
        self.assertEqual(normalise_domain(""), "")

    def test_document_vs_hub_url_classification(self):
        self.assertTrue(is_document_url("https://x.org/a/b/c/guideline.pdf"))
        self.assertTrue(is_document_url("https://x.org/guidelines/lung/sclc"))
        self.assertFalse(is_document_url("https://kce.fgov.be"))
        self.assertFalse(is_document_url("https://www.ema.europa.eu/en/medicines"))


# ===========================================================================
# A1 / A2 — input handling
# ===========================================================================

class TestInputValidation(unittest.TestCase):

    def test_missing_mandatory_field_blocks(self):
        result = inputs.validate_pi({"indication_disease": ""}, {"product_name_inn": "X"})
        self.assertEqual(result.overall_status, "BLOCKED")
        self.assertFalse(result.passed)

    def test_check_items_do_not_block(self):
        result = inputs.validate_pi(
            {"indication_disease": "NSCLC"}, {"product_name_inn": "X"},
            added_fields=["age_group", "sex"])
        self.assertEqual(result.overall_status, "PASS")
        self.assertTrue(result.check_items)

    def test_field_never_added_is_not_a_check_item(self):
        result = inputs.validate_pi({"indication_disease": "NSCLC"},
                                    {"product_name_inn": "X"}, added_fields=[])
        self.assertEqual(result.check_items, [])


class TestInputStructuring(unittest.TestCase):

    def test_therapeutic_class_is_never_inferred(self):
        """SME Agent 2 rule 5, absolute. An inferred class is how a comparator
        ends up wearing the intervention's mechanism."""
        llm = ScriptedLLM({"a02.input_structuring": json.dumps({
            "populations": [],
            "intervention": {"fields": {"therapeutic_class_mechanism": {
                "value": "EGFR TKI", "provenance": "inferred",
                "inference_basis": "guessed from the drug name"}}}})})
        _, intervention = inputs.structure_pi(
            {"indication_disease": "NSCLC"}, {"product_name_inn": "X"}, "", llm)
        self.assertIsNone(intervention.fields["therapeutic_class_mechanism"].value)

    def test_user_statement_beats_inference(self):
        llm = ScriptedLLM({"a02.input_structuring": json.dumps({
            "populations": [{"population_id": "licensed", "fields": {
                "stage_severity": {"value": "WRONG", "provenance": "inferred"}}}],
            "intervention": {"fields": {}}})})
        pops, _ = inputs.structure_pi(
            {"indication_disease": "NSCLC", "stage_severity": "Stage IV"},
            {"product_name_inn": "X"}, "", llm)
        self.assertEqual(pops[0].value("stage_severity"), "Stage IV")

    def test_itt_declaration_creates_a_second_population(self):
        pops, _ = inputs.structure_pi(
            {"indication_disease": "SCLC",
             "itt_differs": "No, it differs — a broader relapsed population"},
            {"product_name_inn": "X"}, "", None)
        self.assertEqual(len(pops), 2)
        self.assertEqual(pops[1].population_id, "intended_to_treat")

    def test_itt_same_does_not_create_a_second_population(self):
        pops, _ = inputs.structure_pi(
            {"indication_disease": "SCLC", "itt_differs": "Yes, same"},
            {"product_name_inn": "X"}, "", None)
        self.assertEqual(len(pops), 1)


# ===========================================================================
# A3 — scope boundary
# ===========================================================================

class TestScopeBoundary(unittest.TestCase):

    def _population(self, **fields) -> Population:
        pop = Population()
        for k in inputs.POPULATION_FIELDS:
            pop.fields[k] = Field(value=fields.get(k), provenance="confirmed"
                                  if fields.get(k) else "not_provided")
        return pop

    def test_unstated_facets_never_become_filters(self):
        """A facet nobody specified must not silently reject evidence."""
        boundary = inputs.build_scope_boundary(
            self._population(indication_disease="SCLC"),
            Intervention(), LicensedIndicationRecord(), None)
        self.assertIn("age_group", boundary.unbounded_facets)
        self.assertNotIn("age_group", [f.name for f in boundary.must_match_facets()])

    def test_discriminating_stated_facets_are_must_match(self):
        boundary = inputs.build_scope_boundary(
            self._population(indication_disease="SCLC",
                             prior_therapy_line="progressed after platinum"),
            Intervention(), LicensedIndicationRecord(), None)
        names = [f.name for f in boundary.must_match_facets()]
        self.assertIn("prior_therapy_line", names)

    def test_free_text_interval_is_lifted_into_a_named_facet(self):
        """An axis with no SME field (a treatment-free interval) must survive as
        a comparable facet rather than being lost in prose."""
        llm = ScriptedLLM({"a03.scope_facet_normalise": json.dumps({"facets": [
            {"name": "treatment_free_interval", "value": ">= 90 days",
             "discriminating": True, "verbatim": "chemotherapy-free interval >= 90 days"}]})})
        boundary = inputs.build_scope_boundary(
            self._population(indication_disease="SCLC",
                             other_characteristics="chemotherapy-free interval >= 90 days"),
            Intervention(), LicensedIndicationRecord(), llm)
        facet = boundary.facets["treatment_free_interval"]
        self.assertTrue(facet.is_bound)
        self.assertTrue(facet.must_match)


# ===========================================================================
# A5 — therapeutic area
# ===========================================================================

class TestTherapeuticArea(unittest.TestCase):

    def _pop(self, indication, orphan=""):
        pop = Population()
        pop.fields["indication_disease"] = Field(value=indication, provenance="confirmed")
        pop.fields["orphan_rare_disease"] = Field(value=orphan or None,
                                                  provenance="confirmed" if orphan else "not_provided")
        for k in ("disease_subtype_histology", "icd_disease_code", "therapeutic_area"):
            pop.fields[k] = Field()
        return pop

    def test_single_area_resolves_without_an_llm_call(self):
        llm = ScriptedLLM({})
        result = inputs.resolve_therapeutic_areas(self._pop("small cell lung cancer"), llm)
        self.assertIn("Oncology", result.areas)
        self.assertEqual(len(llm.call_log), 0, "a confident keyword match must not spend a call")

    def test_orphan_flag_always_adds_rare_diseases(self):
        result = inputs.resolve_therapeutic_areas(
            self._pop("small cell lung cancer", orphan="Yes"), None)
        self.assertIn(C.RARE_DISEASES, result.areas)

    def test_area_is_always_resolved(self):
        result = inputs.resolve_therapeutic_areas(self._pop("a condition with no keywords"), None)
        self.assertTrue(result.areas, "the SME is explicit that there is no 'nothing fits'")


# ===========================================================================
# A6 — query planning
# ===========================================================================

class TestQueryPlanning(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.inv = load_source_inventory(FIXTURE_WORKBOOK, strict=False)

    def _plan(self):
        pop = Population()
        pop.fields["indication_disease"] = Field(value="small cell lung cancer",
                                                 provenance="confirmed")
        for k in inputs.POPULATION_FIELDS:
            pop.fields.setdefault(k, Field())
        inter = Intervention()
        inter.fields["product_name_inn"] = Field(value="Examplimab", provenance="confirmed")
        for k in inputs.INTERVENTION_FIELDS:
            inter.fields.setdefault(k, Field())
        vocab = retrieval.build_vocabulary(pop, ["Oncology"], None)
        return retrieval.plan_queries(pop, inter, ["Oncology"], vocab, self.inv), inter

    def test_every_member_state_is_planned(self):
        plan, _ = self._plan()
        states = {i.member_state for i in plan if i.member_state in C.EU_27_MEMBER_STATES}
        self.assertEqual(len(states), 27, "all 27 states must be assessed on every run")

    def test_states_without_a_curated_hta_source_still_get_searched(self):
        """Six states have no curated HTA domain. 'No curated source' must not
        mean 'not searched'."""
        plan, _ = self._plan()
        for state in ("Malta", "Cyprus", "Luxembourg"):
            items = [i for i in plan if i.member_state == state
                     and i.source_class == C.SRC_HTA_REGULATORY]
            self.assertTrue(items, f"{state} has no HTA query at all")
            self.assertIn(state, items[0].query)

    def test_landscape_pass_exists_and_omits_the_drug(self):
        """A guideline listing the complete treatment-line landscape rarely
        ranks for a drug-anchored query. This is the confirmed cause of the
        missed guideline comparators."""
        plan, inter = self._plan()
        landscape = [i for i in plan if i.pass_type == retrieval.PASS_LANDSCAPE]
        self.assertTrue(landscape, "no landscape pass was planned")
        for item in landscape:
            self.assertNotIn(inter.product_name.lower(), item.query.lower())

    def test_guideline_domains_are_area_filtered(self):
        plan, _ = self._plan()
        onc = set(self.inv.domains(C.SRC_CLINICAL_GUIDELINE, member_state="Austria",
                                   areas=["Oncology"]))
        item = next(i for i in plan if i.member_state == "Austria"
                    and i.source_class == C.SRC_CLINICAL_GUIDELINE)
        self.assertTrue(set(item.domains).issubset(onc))

    def test_vocabulary_cannot_carry_a_drug_name(self):
        """The query planner must not be able to name the answer."""
        llm = ScriptedLLM({"a06.query_vocabulary": json.dumps({
            "indication_synonyms": ["SCLC", "topotecan therapy"],
            "disease_class_terms": ["osimertinib regimens"],
            "indication_abbreviations": [], "localised_assessment_terms": {},
            "outcome_requirement_terms": []})})
        pop = Population()
        pop.fields["indication_disease"] = Field(value="SCLC", provenance="confirmed")
        pop.fields["disease_subtype_histology"] = Field()
        vocab = retrieval.build_vocabulary(pop, ["Oncology"], llm)
        joined = " ".join(vocab.indication_synonyms + vocab.disease_class_terms).lower()
        self.assertNotIn("topotecan", joined)
        self.assertNotIn("osimertinib", joined)


# ===========================================================================
# A8 / A9 / A10 — extraction, grounding, validation
# ===========================================================================

def _record(**kw) -> EvidenceRecord:
    base = dict(finding_id="cmp-1", finding_type=FINDING_COMPARATOR,
                subject_drug="Examplimab", source_id="src-1",
                source_url="https://example.org/doc", source_class=C.SRC_HTA_REGULATORY,
                tier=1, member_state="Germany", grounded=True,
                evidence_quote="topotecan is the appropriate comparator",
                comparator=Comparator(as_stated="Topotecan",
                                      role=C.ROLE_ACTIVE_COMPARATOR),
                population_context=PopulationContext(disease="SCLC"))
    base.update(kw)
    return EvidenceRecord(**base)


class TestGrounding(unittest.TestCase):

    def test_value_present_in_text_is_grounded(self):
        ok, _ = validation.is_grounded("Topotecan", "the comparator is topotecan")
        self.assertTrue(ok)

    def test_invented_value_is_not_grounded(self):
        ok, _ = validation.is_grounded("Pembrolizumab plus lenvatinib",
                                       "the comparator is topotecan")
        self.assertFalse(ok)

    def test_quote_window_prefers_numerals(self):
        text = ("Irrelevant preamble. " * 20) + "median overall survival was 13.6 months" + (
                " trailing text." * 20)
        window = validation.best_quote_window("overall survival 13.6 months", text)
        self.assertIn("13.6", window)


class TestClaimValidation(unittest.TestCase):

    def _providers(self, verdicts):
        search = FixtureSearchProvider(documents={"https://example.org/doc": "topotecan " * 50})
        llm = ScriptedLLM({"a10.claim_validation": json.dumps({"results": verdicts})})
        return search, llm

    def test_a_different_subject_drug_is_rejected_without_an_llm_call(self):
        """A document about extensive-stage SCLC discusses several drugs. Without
        subject_drug, one drug's comparator silently becomes another's."""
        rec = _record(subject_drug="Otherdrugimab")
        search, llm = self._providers([])
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertEqual(rec.validation.verdict, C.V_WRONG_SUBJECT_DRUG)
        self.assertEqual(len(llm.call_log), 0)

    def test_the_intervention_cannot_be_its_own_comparator(self):
        rec = _record(comparator=Comparator(as_stated="Examplimab",
                                            role=C.ROLE_ACTIVE_COMPARATOR))
        search, llm = self._providers([])
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertEqual(rec.validation.verdict, C.V_WRONG_INTERVENTION)

    def test_ungrounded_record_never_reaches_the_validator(self):
        rec = _record(grounded=False)
        search, llm = self._providers([])
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertFalse(rec.validation.passed)

    def test_unparseable_validator_response_fails_closed(self):
        """A malformed batch response must never pass a finding through."""
        rec = _record()
        search = FixtureSearchProvider(documents={"https://example.org/doc": "topotecan " * 50})
        llm = ScriptedLLM({"a10.claim_validation": "not json at all"})
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertEqual(rec.validation.verdict, C.V_NOT_SUPPORTED)

    def test_inaccessible_source_is_distinct_from_unsupported(self):
        rec = _record()
        search = FixtureSearchProvider(documents={})  # URL not present
        llm = ScriptedLLM({})
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertEqual(rec.validation.verdict, C.V_SOURCE_INACCESSIBLE)

    def test_validation_refetches_the_source(self):
        rec = _record()
        search = FixtureSearchProvider(documents={"https://example.org/doc": "topotecan " * 50})
        llm = ScriptedLLM({"a10.claim_validation": json.dumps(
            {"results": [{"index": 1, "verdict": "SUPPORTED", "reason": "ok"}]})})
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        validation.validate_claims([rec], "SCLC", inter, search, llm)
        self.assertTrue(rec.validation.refetched,
                        "the SME requires the cited source to be re-opened")


# ===========================================================================
# A11 — comparator identity. The class defect.
# ===========================================================================

class TestComparatorIdentity(unittest.TestCase):

    def test_class_comes_from_the_comparators_own_inn(self):
        """THE defect: every comparator in all three delivered files carried the
        assessed drug's mechanism."""
        rec = _record(comparator=Comparator(as_stated="Topotecan"))
        identities = validation.resolve_identities([rec], None)
        validation.apply_identities([rec], identities)
        self.assertEqual(rec.comparator.inn, "topotecan")
        self.assertEqual(rec.comparator.class_mechanism, "topoisomerase I inhibitor")
        self.assertEqual(rec.comparator.class_source, "atc_vocabulary")

    def test_extraction_never_sets_a_comparator_class(self):
        """Even if the model returns one, the extraction boundary clears it."""
        doc_text = "The comparator was topotecan in the second line."
        llm = ScriptedLLM({"a08.extraction": json.dumps([{
            "finding_type": "comparator", "subject_drug": "Examplimab",
            "comparator": {"as_stated": "Topotecan", "role": "active_comparator",
                           "class_mechanism": "bispecific T-cell engager"},
            "population_context": {"disease": "SCLC"},
            "evidence_quote": "The comparator was topotecan"}])})
        from jca_phase1.schema import RetrievedDocument
        doc = RetrievedDocument(url="https://x.org/a/b/c", resolved_url="https://x.org/a/b/c",
                                text=doc_text, ok=True, source_class=C.SRC_HTA_REGULATORY)
        pop = Population(fields={"indication_disease": Field(value="SCLC", provenance="confirmed")})
        inter = Intervention(fields={"product_name_inn": Field(value="Examplimab",
                                                               provenance="confirmed")})
        recs = retrieval._extract_one(doc, pop, inter, llm)
        self.assertTrue(recs)
        self.assertEqual(recs[0].comparator.class_mechanism, "",
                         "extraction must never populate a comparator class")

    def test_brand_name_resolves_to_inn(self):
        rec = _record(comparator=Comparator(as_stated="Tagrisso"))
        identities = validation.resolve_identities([rec], None)
        validation.apply_identities([rec], identities)
        self.assertEqual(rec.comparator.inn, "osimertinib")

    def test_combinations_key_on_the_complete_component_set(self):
        """Two regimens sharing one ingredient are two regimens, never a third
        synthesised one."""
        a = Comparator(as_stated="A + B + C", is_combination=True,
                       components=["a", "b", "c"])
        b = Comparator(as_stated="D + C", is_combination=True, components=["d", "c"])
        self.assertNotEqual(validation.identity_key(a), validation.identity_key(b))

    def test_same_combination_worded_differently_merges(self):
        a = Comparator(as_stated="carboplatin + etoposide", is_combination=True,
                       components=["carboplatin", "etoposide"])
        b = Comparator(as_stated="etoposide plus carboplatin", is_combination=True,
                       components=["etoposide", "carboplatin"])
        self.assertEqual(validation.identity_key(a), validation.identity_key(b))

    def test_unresolved_identity_is_marked_not_guessed(self):
        rec = _record(comparator=Comparator(as_stated="Some unlisted regimen"))
        identities = validation.resolve_identities([rec], None)
        validation.apply_identities([rec], identities)
        self.assertEqual(rec.comparator.class_source, "unresolved")
        self.assertEqual(rec.comparator.class_mechanism, "")


# ===========================================================================
# A12 — scope adjudication
# ===========================================================================

class TestScopeAdjudication(unittest.TestCase):

    def test_prior_therapy_only_is_rejected_deterministically(self):
        """The induction-phase backbone trap: treatment history, not a
        comparator for the later phase."""
        rec = _record(comparator=Comparator(as_stated="Carboplatin + etoposide",
                                            role=C.ROLE_PRIOR_THERAPY))
        adj = validation.adjudicate_scope("k", rec.comparator, [rec],
                                          __import__("jca_phase1.schema", fromlist=["x"]).ScopeBoundary(),
                                          Intervention(), None)
        self.assertEqual(adj.verdict, C.SCOPE_OUT)
        self.assertEqual(adj.decisive_facet, "role")

    def test_evidence_against_is_retained(self):
        rec = _record(comparator=Comparator(as_stated="X", role=C.ROLE_BACKGROUND))
        from jca_phase1.schema import ScopeBoundary
        adj = validation.adjudicate_scope("k", rec.comparator, [rec], ScopeBoundary(),
                                          Intervention(), None)
        self.assertTrue(adj.evidence_against,
                        "an exclusion must be reviewable, not a silent drop")

    def test_no_adjudicator_yields_uncertain_not_in_scope(self):
        rec = _record()
        from jca_phase1.schema import ScopeBoundary
        adj = validation.adjudicate_scope("k", rec.comparator, [rec], ScopeBoundary(),
                                          Intervention(), None)
        self.assertEqual(adj.verdict, C.SCOPE_UNCERTAIN)


# ===========================================================================
# A13 — per-state verdicts
# ===========================================================================

class TestMemberStateAssignment(unittest.TestCase):

    def test_every_comparator_gets_27_verdicts(self):
        """The UI renders all 27 states with a two-value legend, so 'absent'
        must never be ambiguous."""
        verdicts = validation.assign_member_states([_record()], C.EU_27_MEMBER_STATES)
        self.assertEqual(len(verdicts), 27)
        self.assertEqual({v.member_state for v in verdicts}, set(C.EU_27_MEMBER_STATES))

    def test_state_with_evidence_is_standard_of_care(self):
        verdicts = validation.assign_member_states([_record(member_state="Germany")],
                                                   C.EU_27_MEMBER_STATES)
        germany = next(v for v in verdicts if v.member_state == "Germany")
        self.assertEqual(germany.verdict, C.STATE_STANDARD_OF_CARE)

    def test_states_without_evidence_are_not_established_not_not_used(self):
        """Asserting 'not used' would be a claim no source supports."""
        verdicts = validation.assign_member_states([_record(member_state="Germany")],
                                                   C.EU_27_MEMBER_STATES)
        malta = next(v for v in verdicts if v.member_state == "Malta")
        self.assertEqual(malta.verdict, C.STATE_NOT_ESTABLISHED)
        self.assertTrue(malta.reason)


# ===========================================================================
# A15 — the outcome catalog
# ===========================================================================

class TestOutcomeCatalog(unittest.TestCase):

    def test_catalog_has_eleven_items(self):
        self.assertEqual(len(consolidation.CATALOG_ITEMS), 11)

    def test_catalog_provenance_is_recorded_as_not_sme(self):
        """The catalog is a MadeAI platform artifact, not an SME requirement.
        The data file must keep saying so."""
        prov = consolidation.CATALOG["provenance"]
        self.assertIn("NOT defined in the SME base prompt", prov["IMPORTANT"])

    def test_synonyms_match_deterministically(self):
        self.assertEqual(consolidation.match_catalog("OS")["catalog_id"], "OS")
        self.assertEqual(consolidation.match_catalog("overall survival")["catalog_id"], "OS")
        self.assertEqual(
            consolidation.match_catalog("serious adverse event")["catalog_id"], "AE_SERIOUS")

    def test_duration_of_response_is_not_a_catalog_item(self):
        """The legacy prompt named Duration of response and Complete response
        rate as catalog terms; the Ground Truth marks both UNLISTED."""
        self.assertIsNone(consolidation.match_catalog("Duration of response"))
        self.assertIsNone(consolidation.match_catalog("Complete response rate"))

    def test_catalog_block_lists_every_item_for_the_model(self):
        """A model cannot map to a catalog it has never seen."""
        block = consolidation.catalog_prompt_block()
        for item in consolidation.CATALOG_ITEMS:
            self.assertIn(item["catalog_id"], block)

    def test_coverage_is_reported_for_every_catalog_item(self):
        view = consolidation.build_outcomes([], None)
        self.assertEqual(len(view.catalog_coverage), 11)
        self.assertTrue(all(c.status == C.EV_NONE for c in view.catalog_coverage))

    def test_missing_outcome_is_a_reported_status_not_an_absence(self):
        view = consolidation.build_outcomes([("Overall survival", "OS", [
            _record(finding_type="outcome", comparator=None,
                    outcome=__import__("jca_phase1.schema", fromlist=["x"]).OutcomeMention(
                        measure="Overall survival", unit="months"))])], None)
        qol = [c for c in view.catalog_coverage if c.category == C.CAT_QOL]
        self.assertTrue(qol)
        self.assertTrue(all(c.status == C.EV_NONE for c in qol))
        self.assertTrue(all(c.detail for c in qol), "a gap must carry an explanation")


# ===========================================================================
# Schema invariants
# ===========================================================================

class TestInvariants(unittest.TestCase):

    def test_comparator_without_evidence_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            ConsolidatedComparator(generic_name="Topotecan", origin=ORIGIN_AGENT)

    def test_user_added_comparator_needs_no_evidence(self):
        c = ConsolidatedComparator(generic_name="Topotecan", origin="user_added")
        self.assertEqual(c.generic_name, "Topotecan")

    def test_comparator_class_from_an_illegal_source_is_rejected(self):
        with self.assertRaises(ValueError):
            ConsolidatedComparator(
                generic_name="Topotecan", origin="user_added",
                class_or_mechanism="bispecific T-cell engager",
                class_source="intervention")

    def test_member_state_summary_must_sum_to_27(self):
        with self.assertRaises(ValueError):
            MemberStateSummary(identified_count=10, not_identified_count=10)

    def test_phase1_output_has_no_pico_sets(self):
        from jca_phase1.schema import Phase1Output
        d = Phase1Output().to_dict()
        self.assertIsNone(d["pico_sets"])
        self.assertIn("Phase 2", d["pico_sets_note"])


# ===========================================================================
# Leakage guard
# ===========================================================================

class TestLeakageGuard(unittest.TestCase):

    def test_target_drug_jca_report_is_blocked(self):
        from jca_phase1.schema import LeakageGuard
        guard = LeakageGuard(drug_tokens=["tarlatamab"])
        self.assertTrue(guard.is_blocked(
            "https://health.ec.europa.eu/document/hta_jca_mp_202417_tarlatamab_report_en.pdf"))

    def test_generic_eu_guidance_on_the_same_domain_is_not_blocked(self):
        """Blocking the whole domain would also block legitimate EU HTA
        methodology guidance, which is not an answer key."""
        from jca_phase1.schema import LeakageGuard
        guard = LeakageGuard(drug_tokens=["tarlatamab"])
        self.assertFalse(guard.is_blocked(
            "https://health.ec.europa.eu/document/hta-methodological-guidance-outcomes_en.pdf"))

    def test_guard_can_be_disabled_for_evaluation_and_is_recorded(self):
        from jca_phase1.schema import LeakageGuard
        guard = LeakageGuard(drug_tokens=["tarlatamab"], allow_jca_reports=True)
        self.assertFalse(guard.is_blocked(
            "https://health.ec.europa.eu/document/hta_jca_mp_202417_tarlatamab_report_en.pdf"))
        self.assertTrue(guard.to_dict()["allow_jca_reports"])


# ===========================================================================
# Balanced selection
# ===========================================================================

class TestBalancedSelection(unittest.TestCase):

    def test_no_category_is_starved(self):
        hits = [SearchHit(url=f"https://a.org/{i}") for i in range(5)]
        hits += [SearchHit(url="https://b.org/1")]
        selected = select_balanced(hits, 4, {"a": ["a.org"], "b": ["b.org"]})
        self.assertIn("https://b.org/1", selected)


# ===========================================================================
# End to end
# ===========================================================================

class TestEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["JCA_SOURCE_WORKBOOK"] = FIXTURE_WORKBOOK
        from jca_phase1.demo import run_demo
        cls.out = run_demo()

    def test_run_completes_and_all_assertions_pass(self):
        failed = [a.name for a in self.out.completeness.assertions if not a.passed]
        self.assertEqual(failed, [], f"failed assertions: {failed}")

    def test_comparators_were_found(self):
        self.assertTrue(self.out.comparators)

    def test_no_comparator_carries_the_intervention_class(self):
        intervention_class = self.out.intervention.value("therapeutic_class_mechanism")
        for c in self.out.comparators:
            self.assertNotEqual(c.class_or_mechanism, intervention_class)

    def test_by_member_state_has_exactly_27_entries(self):
        self.assertEqual(len(self.out.by_member_state), 27)

    def test_unidentified_states_use_the_exact_sme_wording(self):
        for entry in self.out.by_member_state:
            if entry.status == "not_identified":
                self.assertEqual(entry.finding, C.NOT_IDENTIFIED_COMPARATOR_TEXT)

    def test_induction_regimen_was_excluded_with_a_reason(self):
        reasons = " ".join(e["reason"] for e in self.out.validation.to_dict()["excluded"])
        self.assertIn("prior therapy", reasons.lower())

    def test_run_manifest_records_what_is_needed_to_reproduce_the_run(self):
        m = self.out.run_manifest
        for key in ("prompt_versions", "outcome_catalog_version", "source_workbook",
                    "leakage_guard", "therapeutic_areas_applied", "model"):
            self.assertIn(key, m)
        self.assertFalse(m["phase2_included"])

    def test_output_serialises_to_json(self):
        json.dumps(self.out.to_dict(), default=str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
