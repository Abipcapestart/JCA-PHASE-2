"""Composes the three layers - editable instructions, runtime context,
fixed output contract - into the final prompt sent to the LLM. This is the
ONE place that assembly happens; no agent module hand-builds its own prompt
string, so the separation (task Section 12/18) is structural, not just a
convention someone could forget.

NOTE on `source_comparator_refs` (Agent 2's output schema, prompts/
output_schemas.py): this field is an ENGINEERING ADDITION not present in the
SME's original JSON schema - added to make cross-state attachment
(cross_state_attachment.py) and full evidence traceability (task Section 27)
possible without re-asking the LLM to re-derive which original comparators
fed into a combination. This is exactly the kind of "structured JSON output /
schema enforcement / traceability" addition the task explicitly permits
around the SME's business methodology (task Section 1) - the consolidation
LOGIC itself is untouched.
"""

from __future__ import annotations

from prompts.output_schemas import OUTPUT_SCHEMA_BY_AGENT
from prompts.registry import get_registry

_RESPONSE_DISCIPLINE = (
    "\n\n---\n\nOUTPUT FORMAT (fixed - engineering-controlled, matches this exactly):\n\n"
    "{schema}\n\n"
    "Respond with ONLY the JSON object described above. No preamble, no explanation, "
    "no markdown code fences, before or after it."
)


def build_system_prompt(agent_id: str) -> str:
    registry = get_registry()
    active = registry.get_active(agent_id)
    schema_text = OUTPUT_SCHEMA_BY_AGENT[agent_id]
    return active.instruction_text + _RESPONSE_DISCIPLINE.format(schema=schema_text)


def active_prompt_version(agent_id: str) -> int:
    return get_registry().get_active(agent_id).version
