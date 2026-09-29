from excel_export import build_workbook
from schemas import (
    ContextLockingOutput, InterventionInput, MemberStateCountCheck, Phase2Input, Phase2PicoSet,
    Phase2RunResult, PicoConsolidationOutput, PopulationInstance, PopulationResult, SelectedComparator,
    SetAssemblyOutput, SetValidationResult, ValidationOutput,
)

from helpers import make_comparator


def _build_sample_result_and_input():
    phase2_input = Phase2Input(
        run_id="run1",
        intervention=InterventionInput(product_name="Osimertinib"),
        populations=[PopulationInstance(
            population_label="licensed", indication_disease="NSCLC",
            comparators=[make_comparator("Gefitinib", [("Denmark", "unique", "unconfirmed")])],
            outcomes=[],
        )],
    )
    selected = SelectedComparator(generic_name="Gefitinib", combination_type="single",
                                   member_states=["Denmark"], selection_basis="unique", tiers=[1],
                                   source_comparator_refs=["Gefitinib"])
    pico_set = Phase2PicoSet(pico_set_id="gefitinib", comparator="Gefitinib", intervention="Osimertinib",
                              outcomes=["Overall survival"], member_states=["Denmark"], tiers=[1],
                              rationale="test rationale", source_comparator_refs=["Gefitinib"])
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


def test_workbook_has_all_8_sheets():
    result, phase2_input = _build_sample_result_and_input()
    wb = build_workbook(result, phase2_input)
    sheet_names = wb.sheetnames
    assert len(sheet_names) == 8
    assert any("Run Summary" in s for s in sheet_names)
    assert any("PICO Sets" in s for s in sheet_names)
    assert any("Evidence" in s for s in sheet_names)


def test_pico_sets_sheet_contains_expected_row():
    result, phase2_input = _build_sample_result_and_input()
    wb = build_workbook(result, phase2_input)
    ws = wb["5. Agent3 PICO Sets"]
    rows = list(ws.iter_rows(values_only=True))
    header, data_row = rows[0], rows[1]
    assert "Comparator (C)" in header
    assert "Gefitinib" in data_row


def test_evidence_sheet_has_traceable_quote():
    result, phase2_input = _build_sample_result_and_input()
    wb = build_workbook(result, phase2_input)
    ws = wb["7. Evidence Sources"]
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) > 1
    assert any("Gefitinib evidence for Denmark" in str(cell) for row in rows[1:] for cell in row)
