import pytest
from pydantic import ValidationError

from schemas import InterventionInput, MemberStateComparatorRequirement, Phase2Input

from helpers import make_comparator, make_population


def test_valid_member_state_accepted():
    req = MemberStateComparatorRequirement(member_state="Germany", tier=1, comparator_scenario="unique")
    assert req.member_state == "Germany"


def test_case20_invalid_non_eu_member_state_rejected():
    with pytest.raises(ValidationError):
        MemberStateComparatorRequirement(member_state="Switzerland", tier=1, comparator_scenario="unique")


def test_member_state_alias_canonicalized():
    req = MemberStateComparatorRequirement(member_state="czechia", tier=1, comparator_scenario="unique")
    assert req.member_state == "Czech Republic"


def test_phase2_input_requires_at_least_one_population():
    with pytest.raises(ValidationError):
        Phase2Input(run_id="r1", intervention=InterventionInput(product_name="Drug"), populations=[])


def test_population_display_name():
    pop = make_population("NSCLC", [make_comparator("Gefitinib", [("Denmark", "unique", "unconfirmed")])])
    assert "NSCLC" in pop.population_display_name
    assert "licensed" in pop.population_display_name
