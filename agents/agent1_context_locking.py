"""Agent 1 - Context/Methodology Locking.

Per the SME prompt, this agent "carries no clinical or evidentiary judgment
of its own" - it only confirms the methodology genuinely loaded. Implemented
as a deterministic check (PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md
Section 10): no LLM call is made at all. If a future guidance document
genuinely requires free-text version extraction beyond what methodology.py's
regex can find, that would be the one legitimate place to add a small LLM
call - not needed for the current static PDF (see methodology.py).
"""

from __future__ import annotations

from methodology import load_methodology
from schemas import ContextLockingOutput


def run_agent1() -> ContextLockingOutput:
    ctx = load_methodology()
    return ContextLockingOutput(
        methodology_loaded=ctx.methodology_loaded,
        guidance_version=ctx.guidance_version,
        sections_covered=ctx.sections_covered,
    )
