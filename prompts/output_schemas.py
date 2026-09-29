"""Fixed, engineering-controlled output-schema text, reproduced VERBATIM from
the SME Phase 2 prompt docx. Never editable through the SME prompt UI - see
prompts/registry.py (only `instruction_text` is SME-editable) and
prompt_builder.py (composes instruction_text + this fixed block at call
time, per the architecture's mandatory input/output-contract separation).
"""

CONTEXT_LOCKING_OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "methodology_loaded": { "type": "boolean" },
    "guidance_version": { "type": "string" },
    "sections_covered": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["methodology_loaded", "guidance_version", "sections_covered"]
}"""

PICO_CONSOLIDATION_OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "population": { "type": "string" },
    "selected_comparators": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "generic_name": { "type": "string" },
          "combination_type": { "enum": ["single", "or_combination", "must_retain_combination", "individualised_bundle"] },
          "member_states": { "type": "array", "items": { "type": "string" } },
          "selection_basis": { "enum": ["unique", "each_required", "at_least_one", "must_retain_all", "individualised"] },
          "tiers": { "type": "array", "items": { "enum": [1, 2, 3] } },
          "droppable_note": { "type": "string", "description": "Present only when any contributing state's droppability was unconfirmed." },
          "source_comparator_refs": { "type": "array", "items": { "type": "string" }, "description": "The generic_name(s) of every input comparator that make up this selection - a single name for 'single', multiple for any combination type. Required for traceability." },
          "tie_break": {
            "type": ["object", "null"],
            "description": "ENGINEERING ADDITION for auditability, not part of the SME's original schema (task requirement: make automatic tie-breaks auditable rather than silent - see Agent 2 instructions). Populate ONLY when the tie-breaking rule (strongest evidence tier, then broadest Member State applicability) actually had to be applied because more than one equally-minimal comparator combination existed; otherwise omit or set null.",
            "properties": {
              "tie_existed": { "type": "boolean" },
              "candidate_combinations": { "type": "array", "items": { "type": "string" } },
              "selected_combination": { "type": "string" },
              "reason": { "type": "string" },
              "evidence_tier_basis": { "type": "array", "items": { "type": "integer" } },
              "member_state_coverage_basis": { "type": "integer" }
            }
          }
        },
        "required": ["generic_name", "combination_type", "member_states", "selection_basis", "source_comparator_refs"]
      }
    }
  },
  "required": ["population", "selected_comparators"]
}"""

SET_ASSEMBLY_OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "population": { "type": "string" },
    "pico_sets": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "pico_set_id": { "type": "string", "description": "A stable slug derived from the comparator name (lowercase, hyphenated)." },
          "comparator": { "type": "string" },
          "intervention": { "type": "string", "description": "Identical value in every set for this population." },
          "outcomes": { "type": "array", "items": { "type": "string" }, "description": "Identical list in every set for this population." },
          "member_states": { "type": "array", "items": { "type": "string" } },
          "tiers": { "type": "array", "items": { "enum": [1, 2, 3] } },
          "rationale": { "type": "string", "description": "One or two sentences, generated fresh at this stage." },
          "combination_type": { "type": "string" },
          "selection_basis": { "type": "string" },
          "source_comparator_refs": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["pico_set_id", "comparator", "intervention", "outcomes", "member_states", "tiers", "rationale"]
      }
    },
    "not_identified_states": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["population", "pico_sets", "not_identified_states"]
}"""

VALIDATION_OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "population": { "type": "string" },
          "set_id": { "type": "string" },
          "validation_result": { "enum": ["passed", "failed_blocked"] },
          "failure_reason": { "type": ["string", "null"] }
        },
        "required": ["population", "set_id", "validation_result"]
      }
    },
    "member_state_count_check": { "type": "object", "properties": { "total": { "const": 27 }, "matches": { "type": "boolean" } } }
  },
  "required": ["results", "member_state_count_check"]
}"""

OUTPUT_SCHEMA_BY_AGENT = {
    "context_locking": CONTEXT_LOCKING_OUTPUT_SCHEMA,
    "pico_consolidation": PICO_CONSOLIDATION_OUTPUT_SCHEMA,
    "set_assembly": SET_ASSEMBLY_OUTPUT_SCHEMA,
    "validation": VALIDATION_OUTPUT_SCHEMA,
}
