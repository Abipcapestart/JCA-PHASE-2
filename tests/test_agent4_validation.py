import agents.agent4_validation as agent4
from llm_client import LLMCallFailed
from p2_config import EU_27_MEMBER_STATES
from schemas import Phase2PicoSet, SetAssemblyOutput

from helpers import make_comparator, make_population


def _pass_semantic(monkeypatch):
    monkeypatch.setattr(
        agent4, "_semantic_check",
        lambda pico_set, methodology_rule_text: agent4._SemanticCheckOutput(grounded=True, traceable=True, reason=""))


def test_semantic_check_failure_blocks_the_set_and_records_a_log_entry(monkeypatch):
    def _always_fail(pico_set, methodology_rule_text):
        raise LLMCallFailed("simulated throttling")
    monkeypatch.setattr(agent4, "_semantic_check", _always_fail)

    bad_set = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                             outcomes=["OS"], member_states=EU_27_MEMBER_STATES, tiers=[1], rationale="r1")
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[bad_set], not_identified_states=[])
    pop = make_population("NSCLC", [make_comparator("Drug A", [(s, "unique", "unconfirmed") for s in EU_27_MEMBER_STATES])])

    result, logs = agent4.run_agent4(pop, assembly, "methodology text")

    assert result.results[0].validation_result == "failed_blocked"
    assert "Semantic validation call failed" in result.results[0].failure_reason

    assert len(logs) == 1
    assert logs[0].agent_id == "validation"
    assert logs[0].level == "error"
    assert logs[0].pico_set_id == "drug-a"
    assert logs[0].error_type == "LLMCallFailed"
    assert "simulated throttling" in logs[0].message


def test_case12_full_27_state_completeness_passes(monkeypatch):
    _pass_semantic(monkeypatch)
    all_states = EU_27_MEMBER_STATES
    set_a = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                           outcomes=["OS"], member_states=all_states[:20], tiers=[1], rationale="r1")
    set_b = Phase2PicoSet(pico_set_id="drug-b", comparator="Drug B", intervention="X",
                           outcomes=["OS"], member_states=all_states[20:], tiers=[1], rationale="r2")
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[set_a, set_b], not_identified_states=[])
    pop = make_population("NSCLC", [
        make_comparator("Drug A", [(s, "unique", "unconfirmed") for s in all_states[:20]]),
        make_comparator("Drug B", [(s, "unique", "unconfirmed") for s in all_states[20:]]),
    ])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    assert result.member_state_count_check.matches is True
    assert all(r.validation_result == "passed" for r in result.results)


def test_member_state_in_multiple_sets_is_legitimate_and_passes(monkeypatch):
    """A Member State can legitimately require comparisons against several
    different comparators for the same population (e.g. Denmark appearing
    in the Gefitinib set AND the Best-supportive-care set) - this is NOT a
    double-counting error. Confirmed directly against the SME prompt's own
    worked sample output, and against a real end-to-end Bedrock run of this
    exact scenario during implementation."""
    _pass_semantic(monkeypatch)
    set_a = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                           outcomes=["OS"], member_states=["Germany", "France"], tiers=[1], rationale="r1")
    set_b = Phase2PicoSet(pico_set_id="drug-b", comparator="Drug B", intervention="X",
                           outcomes=["OS"], member_states=["Germany"], tiers=[1], rationale="r2")
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[set_a, set_b],
                                  not_identified_states=[s for s in EU_27_MEMBER_STATES if s not in ("Germany", "France")])
    pop = make_population("NSCLC", [
        make_comparator("Drug A", [("Germany", "unique", "unconfirmed"), ("France", "unique", "unconfirmed")]),
        make_comparator("Drug B", [("Germany", "unique", "unconfirmed")]),
    ])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    assert result.member_state_count_check.matches is True
    assert all(r.validation_result == "passed" for r in result.results)


def test_state_in_both_a_set_and_not_identified_is_a_genuine_contradiction(monkeypatch):
    _pass_semantic(monkeypatch)
    set_a = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                           outcomes=["OS"], member_states=["Germany"], tiers=[1], rationale="r1")
    assembly = SetAssemblyOutput(
        population="NSCLC", pico_sets=[set_a],
        not_identified_states=["Germany"] + [s for s in EU_27_MEMBER_STATES if s not in ("Germany",)][:25])
    pop = make_population("NSCLC", [make_comparator("Drug A", [("Germany", "unique", "unconfirmed")])])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    assert result.member_state_count_check.matches is False
    assert any("contradiction" in (r.failure_reason or "") for r in result.results)


def test_case18_invented_tier_flags_only_that_set(monkeypatch):
    _pass_semantic(monkeypatch)
    all_states = EU_27_MEMBER_STATES
    ok_set = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                            outcomes=["OS"], member_states=all_states[:26], tiers=[1], rationale="r1",
                            source_comparator_refs=["Drug A"])
    bad_set = Phase2PicoSet(pico_set_id="drug-b", comparator="Drug B", intervention="X",
                             outcomes=["OS"], member_states=[all_states[26]], tiers=[3], rationale="r2",
                             source_comparator_refs=["Drug B"])  # Drug B is really tier 1, not 3
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[ok_set, bad_set], not_identified_states=[])
    pop = make_population("NSCLC", [
        make_comparator("Drug A", [(s, "unique", "unconfirmed") for s in all_states[:26]], tier=1),
        make_comparator("Drug B", [(all_states[26], "unique", "unconfirmed")], tier=1),
    ])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    by_id = {r.set_id: r for r in result.results}
    assert by_id["drug-a"].validation_result == "passed"
    assert by_id["drug-b"].validation_result == "failed_blocked"
    assert "Tier" in by_id["drug-b"].failure_reason


def test_empty_rationale_blocks_set(monkeypatch):
    _pass_semantic(monkeypatch)
    all_states = EU_27_MEMBER_STATES
    bad_set = Phase2PicoSet(pico_set_id="drug-a", comparator="Drug A", intervention="X",
                             outcomes=["OS"], member_states=all_states, tiers=[1], rationale="   ")
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[bad_set], not_identified_states=[])
    pop = make_population("NSCLC", [make_comparator("Drug A", [(s, "unique", "unconfirmed") for s in all_states])])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    assert result.results[0].validation_result == "failed_blocked"


def test_ordering_violation_detected(monkeypatch):
    _pass_semantic(monkeypatch)
    small = Phase2PicoSet(pico_set_id="small", comparator="A", intervention="X", outcomes=["OS"],
                           member_states=["Germany"], tiers=[1], rationale="r")
    big = Phase2PicoSet(pico_set_id="big", comparator="B", intervention="X", outcomes=["OS"],
                         member_states=["Germany", "France"], tiers=[1], rationale="r")
    assembly = SetAssemblyOutput(population="NSCLC", pico_sets=[small, big],  # wrong order: small before big
                                  not_identified_states=[s for s in EU_27_MEMBER_STATES if s not in ("Germany", "France")])
    pop = make_population("NSCLC", [make_comparator("A", [("Germany", "unique", "unconfirmed")])])
    result, logs = agent4.run_agent4(pop, assembly, "methodology text")
    assert all("ordered broadest-first" in (r.failure_reason or "") for r in result.results)
