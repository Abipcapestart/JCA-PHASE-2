from cross_state_attachment import attach_cross_state_members, build_member_state_acceptability_index
from schemas import SelectedComparator

from helpers import make_comparator


def test_task_section10_worked_example():
    # MS1 requires Drug A (unique); MS2 accepts Drug A or Drug B (at_least_one);
    # MS3 requires Drug A (unique). Selecting Drug A must attach all three.
    comparators = [
        make_comparator("Drug A", [
            ("Germany", "unique", "unconfirmed"),
            ("France", "at_least_one", "unconfirmed"),
            ("Italy", "unique", "unconfirmed"),
        ]),
        make_comparator("Drug B", [
            ("France", "at_least_one", "unconfirmed"),
        ]),
    ]
    selected = [SelectedComparator(generic_name="Drug A", combination_type="single",
                                    member_states=["Germany", "Italy"], selection_basis="unique",
                                    source_comparator_refs=["Drug A"])]
    updated = attach_cross_state_members(selected, comparators)
    assert set(updated[0].member_states) == {"Germany", "Italy", "France"}


def test_no_double_counting_when_state_not_acceptable():
    comparators = [
        make_comparator("Drug A", [("Germany", "unique", "unconfirmed")]),
        make_comparator("Drug B", [("France", "unique", "unconfirmed")]),
    ]
    selected = [SelectedComparator(generic_name="Drug A", combination_type="single",
                                    member_states=["Germany"], selection_basis="unique",
                                    source_comparator_refs=["Drug A"])]
    updated = attach_cross_state_members(selected, comparators)
    assert updated[0].member_states == ["Germany"]


def test_combination_uses_all_component_refs():
    comparators = [
        make_comparator("Drug A", [("Germany", "at_least_one", "confirmed_droppable")]),
        make_comparator("Drug B", [("France", "at_least_one", "confirmed_droppable")]),
    ]
    selected = [SelectedComparator(generic_name="Drug A OR Drug B", combination_type="or_combination",
                                    member_states=["Germany", "France"], selection_basis="at_least_one",
                                    source_comparator_refs=["Drug A", "Drug B"])]
    index = build_member_state_acceptability_index(comparators)
    assert index["Germany"] == {"Drug A"}
    assert index["France"] == {"Drug B"}
    updated = attach_cross_state_members(selected, comparators)
    assert set(updated[0].member_states) == {"Germany", "France"}


def test_original_list_not_mutated():
    comparators = [make_comparator("Drug A", [("Germany", "unique", "unconfirmed"), ("France", "unique", "unconfirmed")])]
    selected = [SelectedComparator(generic_name="Drug A", combination_type="single",
                                    member_states=["Germany"], selection_basis="unique",
                                    source_comparator_refs=["Drug A"])]
    original_ref = selected[0]
    updated = attach_cross_state_members(selected, comparators)
    assert original_ref.member_states == ["Germany"]  # unchanged
    assert updated[0].member_states == ["France", "Germany"]
