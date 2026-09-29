import pytest

import agents.agent2_pico_consolidation as agent2
from llm_client import LLMCallFailed
from schemas import PicoConsolidationOutput, SelectedComparator

from helpers import make_comparator, make_population


def _mock_invoke(monkeypatch, output: PicoConsolidationOutput):
    monkeypatch.setattr(agent2, "invoke_structured", lambda *a, **k: output)


def test_case1_unique_scenario(monkeypatch):
    pop = make_population("NSCLC", [make_comparator("Best supportive care", [
        ("Denmark", "unique", "unconfirmed"), ("Italy", "unique", "unconfirmed"),
    ])])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Best supportive care", combination_type="single",
                            member_states=["Denmark", "Italy"], selection_basis="unique",
                            tiers=[1], source_comparator_refs=["Best supportive care"]),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert len(result.selected_comparators) == 1
    assert set(result.selected_comparators[0].member_states) == {"Denmark", "Italy"}


def test_case2_each_required_scenario(monkeypatch):
    pop = make_population("NSCLC", [
        make_comparator("Treatment 3", [("Germany", "each_required", "unconfirmed")]),
        make_comparator("Treatment 4", [("Germany", "each_required", "unconfirmed")]),
    ])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Treatment 3", combination_type="single", member_states=["Germany"],
                            selection_basis="each_required", tiers=[1], source_comparator_refs=["Treatment 3"]),
        SelectedComparator(generic_name="Treatment 4", combination_type="single", member_states=["Germany"],
                            selection_basis="each_required", tiers=[1], source_comparator_refs=["Treatment 4"]),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert len(result.selected_comparators) == 2
    assert {sc.generic_name for sc in result.selected_comparators} == {"Treatment 3", "Treatment 4"}


def test_case4_at_least_one_confirmed_droppable(monkeypatch):
    pop = make_population("NSCLC", [
        make_comparator("Gefitinib", [("Denmark", "at_least_one", "confirmed_droppable")]),
        make_comparator("Erlotinib", [("Denmark", "at_least_one", "confirmed_droppable")]),
    ])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Gefitinib", combination_type="single", member_states=["Denmark"],
                            selection_basis="at_least_one", tiers=[1], source_comparator_refs=["Gefitinib"]),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].droppable_note is None


def test_case5_at_least_one_confirmed_must_retain(monkeypatch):
    pop = make_population("NSCLC", [
        make_comparator("Treatment 1", [("Germany", "at_least_one", "confirmed_must_retain")]),
        make_comparator("Treatment 3", [("Germany", "at_least_one", "confirmed_must_retain")]),
    ])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Treatment 1 OR Treatment 3", combination_type="must_retain_combination",
                            member_states=["Germany"], selection_basis="must_retain_all", tiers=[1],
                            source_comparator_refs=["Treatment 1", "Treatment 3"]),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].combination_type == "must_retain_combination"
    assert set(result.selected_comparators[0].source_comparator_refs) == {"Treatment 1", "Treatment 3"}


def test_case6_individualised_scenario(monkeypatch):
    pop = make_population("NSCLC", [make_comparator("Individualised bundle", [
        ("Poland", "individualised", "unconfirmed"),
    ])])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Individualised bundle", combination_type="individualised_bundle",
                            member_states=["Poland"], selection_basis="individualised", tiers=[1],
                            source_comparator_refs=["Individualised bundle"]),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].combination_type == "individualised_bundle"


def test_case9_tie_break_recorded(monkeypatch):
    pop = make_population("NSCLC", [make_comparator("Drug A", [("Germany", "at_least_one", "confirmed_droppable")])])
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(
            generic_name="Drug A", combination_type="single", member_states=["Germany"],
            selection_basis="at_least_one", tiers=[1], source_comparator_refs=["Drug A"],
            tie_break={"tie_existed": True, "candidate_combinations": ["Drug A", "Drug B"],
                       "selected_combination": "Drug A", "reason": "stronger tier",
                       "evidence_tier_basis": [1], "member_state_coverage_basis": 5},
        ),
    ])
    _mock_invoke(monkeypatch, mocked)
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].tie_break.tie_existed is True


def test_case10_no_comparators_returns_empty_without_llm_call(monkeypatch):
    called = {"n": 0}
    def _fail_if_called(*a, **k):
        called["n"] += 1
        raise AssertionError("Should not call the LLM when there are no comparators")
    monkeypatch.setattr(agent2, "invoke_structured", _fail_if_called)
    pop = make_population("NSCLC", [])
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators == []
    assert called["n"] == 0


def test_case15_malformed_llm_output_raises_llm_call_failed(monkeypatch):
    def _raise(*a, **k):
        raise LLMCallFailed("could not parse")
    monkeypatch.setattr(agent2, "invoke_structured", _raise)
    pop = make_population("NSCLC", [make_comparator("Drug A", [("Germany", "unique", "unconfirmed")])])
    with pytest.raises(LLMCallFailed):
        agent2.run_agent2(pop, "methodology text")
