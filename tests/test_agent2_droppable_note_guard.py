"""Regression test for a real bug found during the mandated real-Bedrock
end-to-end run (task Section 20): the LLM populated `droppable_note` on
unique/each_required selections (droppability is only a real concept for
at_least_one, per the actual HTA guidance), which then correctly tripped
Agent 4's semantic groundedness/traceability check. Fixed at the engineering
layer in two places: (1) the per-Member-State entries sent to the LLM omit
`retain_all_status` entirely for non-at_least_one entries, and (2) a
deterministic post-processing guard clears any `droppable_note` the LLM
still attaches to a non-at_least_one selection regardless."""

import agents.agent2_pico_consolidation as agent2
from schemas import PicoConsolidationOutput, SelectedComparator

from helpers import make_comparator, make_population


def test_droppable_note_stripped_from_unique_selection(monkeypatch):
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Best supportive care", combination_type="single",
                            member_states=["Denmark"], selection_basis="unique", tiers=[1],
                            source_comparator_refs=["Best supportive care"],
                            droppable_note="Droppability unconfirmed for Denmark"),  # LLM incorrectly populated this
    ])
    monkeypatch.setattr(agent2, "invoke_structured", lambda *a, **k: mocked)
    pop = make_population("NSCLC", [make_comparator("Best supportive care", [("Denmark", "unique", "unconfirmed")])])
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].droppable_note is None


def test_droppable_note_preserved_for_at_least_one_selection(monkeypatch):
    mocked = PicoConsolidationOutput(population="NSCLC", selected_comparators=[
        SelectedComparator(generic_name="Gefitinib", combination_type="single", member_states=["Denmark"],
                            selection_basis="at_least_one", tiers=[1], source_comparator_refs=["Gefitinib"],
                            droppable_note="Droppability unconfirmed for Denmark"),
    ])
    monkeypatch.setattr(agent2, "invoke_structured", lambda *a, **k: mocked)
    pop = make_population("NSCLC", [make_comparator("Gefitinib", [("Denmark", "at_least_one", "unconfirmed")])])
    result = agent2.run_agent2(pop, "methodology text")
    assert result.selected_comparators[0].droppable_note == "Droppability unconfirmed for Denmark"


def test_retain_all_status_omitted_from_prompt_for_non_at_least_one():
    pop = make_population("NSCLC", [
        make_comparator("Best supportive care", [("Denmark", "unique", "unconfirmed")]),
        make_comparator("Gefitinib", [("Italy", "at_least_one", "confirmed_droppable")]),
    ])
    prompt = agent2._build_user_prompt(pop, "methodology text")
    # the at_least_one entry's status should appear; the unique entry's should not
    assert '"retain_all_status": "confirmed_droppable"' in prompt
    assert prompt.count('"retain_all_status"') == 1
