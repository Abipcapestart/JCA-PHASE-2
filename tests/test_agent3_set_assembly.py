import agents.agent3_set_assembly as agent3
from schemas import PicoConsolidationOutput, SelectedComparator

from helpers import make_comparator, make_population


def test_deterministic_assembly_with_mocked_rationale(monkeypatch):
    monkeypatch.setattr(agent3, "_generate_rationale", lambda pico_set, grounding, droppable: "mocked rationale")

    pop = make_population("NSCLC", [
        make_comparator("Best supportive care", [("Denmark", "unique", "unconfirmed"),
                                                   ("Italy", "unique", "unconfirmed"),
                                                   ("France", "unique", "unconfirmed")]),
        make_comparator("Gefitinib", [("Denmark", "at_least_one", "unconfirmed")]),
    ])
    consolidation = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Best supportive care", combination_type="single",
                            member_states=["Denmark", "Italy", "France"], selection_basis="unique",
                            tiers=[1], source_comparator_refs=["Best supportive care"]),
        SelectedComparator(generic_name="Gefitinib", combination_type="single", member_states=["Denmark"],
                            selection_basis="at_least_one", tiers=[1], source_comparator_refs=["Gefitinib"]),
    ])
    assembly, logs = agent3.run_agent3(pop, consolidation, intervention_name="TestDrug")

    assert len(assembly.pico_sets) == 2
    # broadest-first ordering
    assert assembly.pico_sets[0].comparator == "Best supportive care"
    assert assembly.pico_sets[0].intervention == "TestDrug"
    assert assembly.pico_sets[0].outcomes == ["Overall survival"]
    assert assembly.pico_sets[0].rationale == "mocked rationale"
    assert assembly.pico_sets[0].pico_set_id == "best-supportive-care"
    assert logs == []


def test_rationale_generation_failure_flags_set_without_crashing(monkeypatch):
    import llm_client

    def _always_fail(*a, **k):
        raise llm_client.LLMCallFailed("simulated content filter")
    monkeypatch.setattr(agent3, "invoke_structured", _always_fail)

    pop = make_population("NSCLC", [make_comparator("Drug A", [("Denmark", "unique", "unconfirmed")])])
    consolidation = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Drug A", combination_type="single", member_states=["Denmark"],
                            selection_basis="unique", tiers=[1], source_comparator_refs=["Drug A"]),
    ])
    assembly, logs = agent3.run_agent3(pop, consolidation, intervention_name="TestDrug")
    assert "FAILED" in assembly.pico_sets[0].rationale

    assert len(logs) == 1
    assert logs[0].agent_id == "set_assembly"
    assert logs[0].level == "error"
    assert logs[0].pico_set_id == assembly.pico_sets[0].pico_set_id
    assert logs[0].error_type == "LLMCallFailed"
    assert "simulated content filter" in logs[0].message


def test_not_identified_states_carried_through():
    consolidation = PicoConsolidationOutput(population="NSCLC", selected_comparators=[])
    pop = make_population("NSCLC", [], not_identified_states=["Malta", "Luxembourg"])
    assembly, logs = agent3.run_agent3(pop, consolidation)
    assert assembly.pico_sets == []
    assert assembly.not_identified_states == ["Malta", "Luxembourg"]
    assert logs == []
