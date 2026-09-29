"""Pydantic schemas for Phase 2 - PICOS Set Consolidation.

Three distinct layers, per the architecture's hard separation requirement
(never mix these into one giant model):

1. Input contract (`Phase2Input` and friends) - what the Phase 1 -> Phase 2
   adapter produces. Field names/shapes are taken directly from Phase 1's
   actual, verified `schema.py` (ConsolidatedComparator, MemberStateFinding,
   ConsolidatedOutcome) - see PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md
   Section 4. This layer is engineering-controlled; SMEs never edit it.

2. Agent output contracts (`ContextLockingOutput`, `PicoConsolidationOutput`,
   `SetAssemblyOutput`, `ValidationOutput`) - reproduced field-for-field from
   the SME's Phase 2 prompt's own JSON schemas. These are the fixed, engine-
   controlled output contracts the SME prompt's instruction text is combined
   with at call time (see prompts/registry.py) - never edited via the prompt
   UI.

3. Final run result (`Phase2RunResult`) - the complete, persisted/exported
   deliverable, engineering-defined.

IMPORTANT NAMING NOTE (explicit, per the architecture's Gap 6 warning):
Phase 1's `FinalScopingOutput.pico_sets` (see phase1_scoping/pico_set_
derivation.py) is a *different, unrelated* concept from this module's
`Phase2PicoSet` / `SetAssemblyOutput.pico_sets`. Phase 1's field is never
read or reused by this package for PICO-set construction - only Phase 1's
`comparators` (ConsolidatedComparator list) is consumed. The external SME-
facing output field is still named `pico_sets` here (per the fixed SME
schema, Section 7 of the architecture doc) - only the *internal* class name
(`Phase2PicoSet`) is deliberately distinct from Phase 1's `PicoSetEntry` to
prevent a future maintainer from confusing the two.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from p2_config import EU_27_MEMBER_STATES, canonicalize_member_state

# ===========================================================================
# 1. Input contract - Phase 1 -> Phase 2 (engineering-controlled)
# ===========================================================================


class ComparatorScenario(str, Enum):
    UNIQUE = "unique"
    EACH_REQUIRED = "each_required"
    AT_LEAST_ONE = "at_least_one"
    INDIVIDUALISED = "individualised"


class RetainAllStatus(str, Enum):
    CONFIRMED_DROPPABLE = "confirmed_droppable"
    CONFIRMED_MUST_RETAIN = "confirmed_must_retain"
    UNCONFIRMED = "unconfirmed"


class OutcomeGroupLabel(str, Enum):
    """The SME Phase 2 prompt's 3 named outcome groups. Phase 1's 4th bucket
    (clinician_patient_reported) is folded into QUALITY_OF_LIFE_AND_SYMPTOMS
    at the adapter layer - see PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md
    Open Question 4 (recommended default, pending SME confirmation)."""
    EFFICACY = "efficacy"
    SAFETY = "safety"
    QUALITY_OF_LIFE_AND_SYMPTOMS = "quality_of_life_and_symptoms"


class EvidenceItem(BaseModel):
    source: str = ""
    quote: str = ""


class MemberStateComparatorRequirement(BaseModel):
    """One (comparator group) x (Member State) pairing - 1:1 with Phase 1's
    MemberStateFinding. This is the core Phase 2 input: the raw per-state
    fact set Agent 2 applies the 4-case methodology to."""
    member_state: str
    tier: int
    source_reference: str = ""
    evidence_quote: str = ""
    comparator_scenario: ComparatorScenario
    retain_all_status: RetainAllStatus = RetainAllStatus.UNCONFIRMED

    @field_validator("member_state")
    @classmethod
    def _canonical_state(cls, v: str) -> str:
        canonical = canonicalize_member_state(v)
        if canonical is None or canonical not in EU_27_MEMBER_STATES:
            raise ValueError(
                f"{v!r} does not resolve to one of the 27 EU Member States - "
                f"never silently dropped or coerced.")
        return canonical


class ComparatorInput(BaseModel):
    """1:1 with Phase 1's ConsolidatedComparator. `rationale` is read-only
    grounding material for Agent 3's fresh rationale - never copied verbatim
    into Phase 2's output (SME's explicit instruction)."""
    generic_name: str
    brand_names: List[str] = Field(default_factory=list)
    member_states: List[str] = Field(default_factory=list)
    per_member_state: List[MemberStateComparatorRequirement] = Field(default_factory=list)
    class_or_mechanism: str = ""
    line_of_therapy: str = ""
    indication: str = ""
    tiers: List[int] = Field(default_factory=list)
    rationale: str = ""
    sources: List[str] = Field(default_factory=list)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    # Forward-compatible only - no Phase 1 workflow populates this today.
    # See PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md Section 5/15 Q6:
    # deliberately NOT wired to any manual-add mechanism in this
    # implementation; always "agent_found" until that capability exists.
    source_type: str = "agent_found"


class OutcomeInput(BaseModel):
    """1:1 with Phase 1's ConsolidatedOutcome, after the 4-bucket ->
    3-group transformation (adapter.py)."""
    concept: str
    group: OutcomeGroupLabel
    tiers: List[int] = Field(default_factory=list)
    rationale: str = ""
    sources: List[str] = Field(default_factory=list)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    listed: bool = True
    source_type: str = "agent_found"


class InterventionInput(BaseModel):
    product_name: str
    attributes: Dict[str, str] = Field(default_factory=dict)  # class, route, dose, schedule, ...


class PopulationInstance(BaseModel):
    """One licensed- or intended-to-treat population instance. Each runs the
    full Agent 2/3/4 pipeline independently (PHASE2_MINIMAL_CHANGE_
    IMPLEMENTATION_PLAN.md Section 16A/Open Question 7)."""
    population_label: str  # "licensed" | "intended_to_treat"
    population_subset_label: str = ""
    indication_disease: str
    attributes: Dict[str, str] = Field(default_factory=dict)  # age_group, sex, stage_severity, ...
    comparators: List[ComparatorInput] = Field(default_factory=list)
    outcomes: List[OutcomeInput] = Field(default_factory=list)
    not_identified_states: List[str] = Field(default_factory=list)  # from Phase 1's scoping-stage coverage
    member_state_summary: Dict[str, int] = Field(default_factory=dict)

    @property
    def population_display_name(self) -> str:
        base = self.population_subset_label or self.indication_disease
        return f"{base} ({self.population_label})" if self.population_label else base


class Phase2Input(BaseModel):
    run_id: str
    source_phase1_run_ref: str = ""  # pointer/ID to the source FinalScopingOutput(s), not a full copy
    intervention: InterventionInput
    populations: List[PopulationInstance]

    @field_validator("populations")
    @classmethod
    def _non_empty(cls, v: List[PopulationInstance]) -> List[PopulationInstance]:
        if not v:
            raise ValueError("Phase2Input requires at least one population instance")
        return v


# ===========================================================================
# 2. Agent output contracts (reproduced verbatim from the SME Phase 2 prompt)
# ===========================================================================


class ContextLockingOutput(BaseModel):
    methodology_loaded: bool
    guidance_version: str
    sections_covered: List[str]


class TieBreakInfo(BaseModel):
    """ENGINEERING ADDITION - not part of the SME's fixed schema, added per
    this implementation's explicit instruction (task Section 11): the SME
    prompt's tie-break rule (prefer strongest tier, then broadest MS
    applicability) conflicts with the actual HTA guidance (surface as
    "highlighted" for MS/assessor discussion, no automatic rule - PDF p.23).
    Both are honored: the SME's rule is applied as the automated decision,
    but every time it actually fires on a genuine tie, that fact is recorded
    here for audit/UI/Excel visibility rather than silently applied."""
    tie_existed: bool
    candidate_combinations: List[str] = Field(default_factory=list)
    selected_combination: str = ""
    reason: str = ""
    evidence_tier_basis: List[int] = Field(default_factory=list)
    member_state_coverage_basis: int = 0


class SelectedComparator(BaseModel):
    generic_name: str
    combination_type: str  # single | or_combination | must_retain_combination | individualised_bundle
    member_states: List[str] = Field(default_factory=list)
    selection_basis: str  # unique | each_required | at_least_one | must_retain_all | individualised
    tiers: List[int] = Field(default_factory=list)
    droppable_note: Optional[str] = None
    tie_break: Optional[TieBreakInfo] = None
    source_comparator_refs: List[str] = Field(default_factory=list)  # traceability -> ComparatorInput.generic_name


class PicoConsolidationOutput(BaseModel):
    population: str
    selected_comparators: List[SelectedComparator] = Field(default_factory=list)


class Phase2PicoSet(BaseModel):
    """See module docstring: this is the FINAL PICO set concept the SME
    prompt calls `pico_sets` - NOT Phase 1's unrelated `pico_sets` field."""
    pico_set_id: str
    comparator: str
    intervention: str
    outcomes: List[str] = Field(default_factory=list)
    member_states: List[str] = Field(default_factory=list)
    tiers: List[int] = Field(default_factory=list)
    rationale: str
    combination_type: str = ""
    selection_basis: str = ""
    tie_break: Optional[TieBreakInfo] = None
    source_comparator_refs: List[str] = Field(default_factory=list)


class SetAssemblyOutput(BaseModel):
    population: str
    pico_sets: List[Phase2PicoSet] = Field(default_factory=list)
    not_identified_states: List[str] = Field(default_factory=list)


class SetValidationResult(BaseModel):
    population: str
    set_id: str
    validation_result: str  # "passed" | "failed_blocked"
    failure_reason: Optional[str] = None


class MemberStateCountCheck(BaseModel):
    total: int = 27
    matches: bool


class ValidationOutput(BaseModel):
    results: List[SetValidationResult] = Field(default_factory=list)
    member_state_count_check: MemberStateCountCheck


class RunLogEntry(BaseModel):
    """A structured record of an agent failing at run time - timestamp,
    which agent, which population/PICO set it was working on, and the
    actual exception type/message (the "cause"), not just a flattened
    string. Replaces the earlier `Phase2RunResult.errors: List[str]`, which
    lost exactly this information (see graph.py, agent3_set_assembly.py,
    agent4_validation.py for where these are recorded)."""
    timestamp: str
    level: str = "error"  # "error" | "warning"
    agent_id: str  # context_locking | pico_consolidation | set_assembly | validation
    population: Optional[str] = None
    pico_set_id: Optional[str] = None
    error_type: str = ""  # exception class name, e.g. "LLMCallFailed", "FileNotFoundError"
    message: str


# ===========================================================================
# 3. Final run result (engineering-defined persistence/export shape)
# ===========================================================================


class PopulationResult(BaseModel):
    population: str
    population_label: str
    consolidation: PicoConsolidationOutput
    assembly: SetAssemblyOutput
    validation: ValidationOutput


class Phase2RunResult(BaseModel):
    run_id: str
    model: str = ""
    prompt_versions: Dict[str, int] = Field(default_factory=dict)  # agent_id -> version used
    context_locking: ContextLockingOutput
    populations: List[PopulationResult] = Field(default_factory=list)
    overall_status: str = "OK"  # "OK" | "BLOCKED"
    logs: List[RunLogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_overall_status(self) -> "Phase2RunResult":
        any_blocked = any(
            r.validation_result == "failed_blocked"
            for pop in self.populations
            for r in pop.validation.results
        )
        any_error_log = any(entry.level == "error" for entry in self.logs)
        if (any_blocked or any_error_log) and self.overall_status == "OK":
            self.overall_status = "BLOCKED"
        return self
