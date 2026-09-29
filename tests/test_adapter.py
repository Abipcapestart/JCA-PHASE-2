import pytest

from adapter import Phase1OutputAdapterError, adapt_orchestrator_result


def _minimal_phase1_output(**overrides):
    base = {
        "input_validation": {"overall_status": "PASS", "fix_items": [], "check_items": []},
        "intervention": {
            "fields": {"product_name_inn": {"value": "Osimertinib", "provenance": "confirmed"}},
            "custom_fields": [],
        },
        "populations": [{
            "population_id": "licensed",
            "fields": {"indication_disease": {"value": "NSCLC", "provenance": "confirmed"}},
            "custom_fields": [],
        }],
        "comparators": [{
            "generic_name": "Gefitinib",
            "member_states": ["Denmark"],
            "per_member_state": [{
                "member_state": "Denmark", "verdict": "standard_of_care", "tier": 1,
                "source_id": "src-1", "source_url": "https://x", "evidence_quote": "q",
            }],
            "tiers": [1], "rationale": "grounding", "class_or_mechanism": "EGFR TKI",
            "comparator_scenario": "unique", "retain_all_status": "unconfirmed",
            "origin": "agent_found",
            "sources": [{"source_id": "src-1", "url": "https://x"}], "evidence": [],
        }],
        "not_identified_states": ["Malta"],
        "member_state_summary": {"identified_count": 1, "not_identified_count": 26, "total": 27},
        "outcomes": {
            "clinical_effectiveness": [{"concept": "Overall survival", "tiers": [1], "rationale": "r",
                                        "sources": [], "evidence": [], "listed": True}],
            "safety": [], "quality_of_life": [],
            "clinician_patient_reported": [
                {"concept": "Patient-reported fatigue", "tiers": [2], "rationale": "r2",
                 "sources": [], "evidence": [], "listed": True}
            ],
        },
    }
    base.update(overrides)
    return base


def test_adapts_a_well_formed_result():
    result = adapt_orchestrator_result(_minimal_phase1_output(), run_id="run1")
    assert result.warnings == []
    p2 = result.phase2_input
    assert p2.run_id == "run1"
    assert p2.intervention.product_name == "Osimertinib"
    assert len(p2.populations) == 1
    pop = p2.populations[0]
    assert pop.population_label == "licensed"
    assert pop.indication_disease == "NSCLC"
    assert len(pop.comparators) == 1
    comp = pop.comparators[0]
    assert comp.generic_name == "Gefitinib"
    assert comp.source_type == "agent_found"
    assert len(comp.per_member_state) == 1
    assert comp.per_member_state[0].comparator_scenario.value == "unique"
    assert comp.sources == ["https://x"]
    # 4-bucket -> 3-group folding: clinician_patient_reported folds into quality_of_life_and_symptoms
    groups = {o.group.value for o in pop.outcomes}
    assert groups == {"efficacy", "quality_of_life_and_symptoms"}


def test_accepts_a_single_dict_as_well_as_a_list():
    single = adapt_orchestrator_result(_minimal_phase1_output(), run_id="run1")
    as_list = adapt_orchestrator_result([_minimal_phase1_output()], run_id="run1")
    assert single.phase2_input.populations[0].indication_disease == \
        as_list.phase2_input.populations[0].indication_disease


def test_blocked_status_raises():
    result = _minimal_phase1_output()
    result["input_validation"]["overall_status"] = "BLOCKED"
    with pytest.raises(Phase1OutputAdapterError):
        adapt_orchestrator_result(result, run_id="run1")


def test_missing_intervention_name_raises():
    result = _minimal_phase1_output()
    result["intervention"] = {"fields": {}, "custom_fields": []}
    with pytest.raises(Phase1OutputAdapterError):
        adapt_orchestrator_result(result, run_id="run1")


def test_no_runs_raises():
    with pytest.raises(Phase1OutputAdapterError):
        adapt_orchestrator_result([], run_id="run1")


def test_unidentified_member_state_string_skipped_with_warning():
    result = _minimal_phase1_output()
    result["comparators"][0]["per_member_state"][0]["member_state"] = "Not A Real Country"
    adaptation = adapt_orchestrator_result(result, run_id="run1")
    assert any("per-Member-State" in w for w in adaptation.warnings)
    # the comparator itself still survives, just without the bad per-state entry
    assert adaptation.phase2_input.populations[0].comparators[0].generic_name == "Gefitinib"
    assert adaptation.phase2_input.populations[0].comparators[0].per_member_state == []


def test_missing_optional_fields_defaults_gracefully():
    result = _minimal_phase1_output()
    result["comparators"][0].pop("class_or_mechanism", None)
    adaptation = adapt_orchestrator_result(result, run_id="run1")
    comp = adaptation.phase2_input.populations[0].comparators[0]
    assert comp.class_or_mechanism == ""


def test_not_established_state_excluded_silently():
    """`not_established`/`not_used` are not genuine per-state requirements
    (see jca_phase1 assign_member_states()) - they must be dropped without
    being treated as a malformed-row warning."""
    result = _minimal_phase1_output()
    result["comparators"][0]["per_member_state"].append({
        "member_state": "France", "verdict": "not_established", "reason": "no evidence",
    })
    adaptation = adapt_orchestrator_result(result, run_id="run1")
    states = {r.member_state for r in adaptation.phase2_input.populations[0].comparators[0].per_member_state}
    assert states == {"Denmark"}
    assert adaptation.warnings == []


def test_blank_comparator_scenario_skipped_with_warning():
    result = _minimal_phase1_output()
    result["comparators"][0]["comparator_scenario"] = ""
    adaptation = adapt_orchestrator_result(result, run_id="run1")
    assert any("comparator_scenario" in w for w in adaptation.warnings)
    assert adaptation.phase2_input.populations[0].comparators[0].per_member_state == []


def test_multiple_phase1_outputs_produce_multiple_populations():
    licensed = _minimal_phase1_output()
    itt = _minimal_phase1_output()
    itt["populations"][0]["population_id"] = "intended_to_treat"
    itt["populations"][0]["fields"]["indication_disease"]["value"] = "NSCLC, broader ITT population"

    adaptation = adapt_orchestrator_result([licensed, itt], run_id="run1")
    assert adaptation.warnings == []
    labels = [p.population_label for p in adaptation.phase2_input.populations]
    assert labels == ["licensed", "intended_to_treat"]


def test_mismatched_intervention_across_runs_warns_and_uses_first():
    licensed = _minimal_phase1_output()
    itt = _minimal_phase1_output()
    itt["populations"][0]["population_id"] = "intended_to_treat"
    itt["intervention"]["fields"]["product_name_inn"]["value"] = "SomeOtherDrug"

    adaptation = adapt_orchestrator_result([licensed, itt], run_id="run1")
    assert adaptation.phase2_input.intervention.product_name == "Osimertinib"
    assert any("differs from run #1" in w for w in adaptation.warnings)
