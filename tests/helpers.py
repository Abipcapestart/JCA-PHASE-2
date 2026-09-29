from schemas import ComparatorInput, MemberStateComparatorRequirement, OutcomeGroupLabel, OutcomeInput, PopulationInstance


def make_comparator(generic_name, states_and_scenarios, tier=1, rationale="grounding rationale"):
    """states_and_scenarios: list of (member_state, comparator_scenario, retain_all_status)."""
    per_ms = [
        MemberStateComparatorRequirement(
            member_state=state, tier=tier, source_reference=f"https://example.org/{state}",
            evidence_quote=f"{generic_name} evidence for {state}",
            comparator_scenario=scenario, retain_all_status=retain,
        )
        for state, scenario, retain in states_and_scenarios
    ]
    return ComparatorInput(
        generic_name=generic_name, member_states=[s for s, _, _ in states_and_scenarios],
        per_member_state=per_ms, tiers=[tier], rationale=rationale,
        sources=[f"https://example.org/{generic_name}"],
    )


def make_population(indication, comparators, outcomes=None, not_identified_states=None):
    outcomes = outcomes or [OutcomeInput(concept="Overall survival", group=OutcomeGroupLabel.EFFICACY, tiers=[1])]
    return PopulationInstance(
        population_label="licensed", indication_disease=indication,
        comparators=comparators, outcomes=outcomes,
        not_identified_states=not_identified_states or [],
    )
