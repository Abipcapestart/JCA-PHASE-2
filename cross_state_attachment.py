"""Deterministic cross-state attachment (task Section 10).

SME rule, verbatim intent: "Once a comparator is selected under any of these
cases, every Member State whose own per-state requirement includes that
exact comparator as an acceptable option is attached to that comparator's
Member State list, regardless of which case originally caused the comparator
to be selected." This is a pure set-union operation over facts Phase 1
already provides (per_member_state entries) - never re-asked of the LLM.

Consistent with the actual HTA guidance's own worked example (PDF Tables 6-9,
pp. 22-25 - Table 8/Figure 4: MS 2's requirement is satisfied by a comparator
selected to meet a *different* MS's must-retain requirement).
"""

from __future__ import annotations

from typing import Dict, List, Set

from p2_config import EU_27_MEMBER_STATES
from schemas import ComparatorInput, SelectedComparator


def build_member_state_acceptability_index(comparators: List[ComparatorInput]) -> Dict[str, Set[str]]:
    """Map each Member State to the set of generic_names it lists as an
    acceptable option anywhere in its own per_member_state requirement."""
    index: Dict[str, Set[str]] = {state: set() for state in EU_27_MEMBER_STATES}
    for comparator in comparators:
        for req in comparator.per_member_state:
            if req.member_state in index:
                index[req.member_state].add(comparator.generic_name)
    return index


def attach_cross_state_members(selected: List[SelectedComparator],
                                comparators: List[ComparatorInput]) -> List[SelectedComparator]:
    """For each Agent 2 selection, union its `member_states` with every state
    whose own acceptability set intersects the selection's component
    comparator names (`source_comparator_refs`, or `generic_name` itself for
    a single/non-combined selection). Does not mutate the input list."""
    index = build_member_state_acceptability_index(comparators)
    updated: List[SelectedComparator] = []
    for sel in selected:
        component_names = set(sel.source_comparator_refs) or {sel.generic_name}
        attached = set(sel.member_states)
        for state, acceptable in index.items():
            if acceptable & component_names:
                attached.add(state)
        updated.append(sel.model_copy(update={"member_states": sorted(attached)}))
    return updated
