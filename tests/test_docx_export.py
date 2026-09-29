from docx_export import build_document
from schemas import (
    ComparatorInput, ContextLockingOutput, InterventionInput, MemberStateComparatorRequirement,
    MemberStateCountCheck, OutcomeGroupLabel, OutcomeInput, Phase2Input, Phase2PicoSet, Phase2RunResult,
    PicoConsolidationOutput, PopulationInstance, PopulationResult, SelectedComparator, SetAssemblyOutput,
    SetValidationResult, ValidationOutput,
)


def _build_sample_result_and_input():
    comparator = ComparatorInput(
        generic_name="Gefitinib", member_states=["Denmark", "Italy"],
        per_member_state=[
            MemberStateComparatorRequirement(member_state="Denmark", tier=1, comparator_scenario="unique"),
            MemberStateComparatorRequirement(member_state="Italy", tier=1, comparator_scenario="unique"),
        ],
        tiers=[1], rationale="grounding rationale",
    )
    phase2_input = Phase2Input(
        run_id="run1",
        intervention=InterventionInput(product_name="Osimertinib"),
        populations=[PopulationInstance(
            population_label="licensed", indication_disease="NSCLC",
            comparators=[comparator],
            outcomes=[
                OutcomeInput(concept="Overall survival", group=OutcomeGroupLabel.EFFICACY, tiers=[1]),
                OutcomeInput(concept="Grade 3+ AEs", group=OutcomeGroupLabel.SAFETY, tiers=[1]),
                OutcomeInput(concept="EQ-5D", group=OutcomeGroupLabel.QUALITY_OF_LIFE_AND_SYMPTOMS,
                              tiers=[1], listed=False),
            ],
        )],
    )
    selected = SelectedComparator(generic_name="Gefitinib", combination_type="single",
                                   member_states=["Denmark", "Italy"], selection_basis="unique", tiers=[1],
                                   source_comparator_refs=["Gefitinib"])
    pico_set = Phase2PicoSet(pico_set_id="gefitinib", comparator="Gefitinib", intervention="Osimertinib",
                              outcomes=["Overall survival", "Grade 3+ AEs", "EQ-5D"],
                              member_states=["Denmark", "Italy"], tiers=[1],
                              rationale="Selected as the unique required comparator.",
                              source_comparator_refs=["Gefitinib"])
    pop_result = PopulationResult(
        population="NSCLC", population_label="licensed",
        consolidation=PicoConsolidationOutput(population="NSCLC", selected_comparators=[selected]),
        assembly=SetAssemblyOutput(population="NSCLC", pico_sets=[pico_set], not_identified_states=["Malta"]),
        validation=ValidationOutput(
            results=[SetValidationResult(population="NSCLC", set_id="gefitinib", validation_result="passed")],
            member_state_count_check=MemberStateCountCheck(total=27, matches=True),
        ),
    )
    result = Phase2RunResult(
        run_id="run1", model="test-model", prompt_versions={"context_locking": 1},
        context_locking=ContextLockingOutput(methodology_loaded=True, guidance_version="v1", sections_covered=["s1"]),
        populations=[pop_result],
    )
    return result, phase2_input


def _table_text(table):
    return [[cell.text for cell in row.cells] for row in table.rows]


def test_document_has_one_table_per_population_with_pico_set_columns():
    result, phase2_input = _build_sample_result_and_input()
    document = build_document(result, phase2_input)
    assert len(document.tables) == 1
    rows = _table_text(document.tables[0])
    assert rows[0] == ["PICO Set (Comparator, C)", "Gefitinib"]


def test_rationale_row_is_never_blank():
    result, phase2_input = _build_sample_result_and_input()
    document = build_document(result, phase2_input)
    rows = _table_text(document.tables[0])
    rationale_row = next(r for r in rows if r[0] == "Rationale")
    assert rationale_row[1] == "Selected as the unique required comparator."


def test_intervention_row_carries_the_real_product_name():
    result, phase2_input = _build_sample_result_and_input()
    document = build_document(result, phase2_input)
    rows = _table_text(document.tables[0])
    intervention_row = next(r for r in rows if r[0] == "Intervention (I)")
    assert intervention_row[1] == "Osimertinib"


def test_outcomes_are_grouped_and_unlisted_is_annotated():
    result, phase2_input = _build_sample_result_and_input()
    document = build_document(result, phase2_input)
    rows = _table_text(document.tables[0])
    efficacy_row = next(r for r in rows if r[0] == "Outcomes (O) — Efficacy")
    safety_row = next(r for r in rows if r[0] == "Outcomes (O) — Safety")
    qol_row = next(r for r in rows if r[0] == "Outcomes (O) — Quality of Life & Symptoms")
    assert "Overall survival" in efficacy_row[1]
    assert "Grade 3+ AEs" in safety_row[1]
    assert "EQ-5D (unlisted)" in qol_row[1]


def test_not_identified_states_appear_as_a_paragraph():
    result, phase2_input = _build_sample_result_and_input()
    document = build_document(result, phase2_input)
    full_text = "\n".join(p.text for p in document.paragraphs)
    assert "Malta" in full_text


def test_population_with_no_pico_sets_does_not_crash():
    result, phase2_input = _build_sample_result_and_input()
    result.populations[0].assembly.pico_sets = []
    document = build_document(result, phase2_input)
    assert len(document.tables) == 0
    full_text = "\n".join(p.text for p in document.paragraphs)
    assert "No PICO sets were produced" in full_text
