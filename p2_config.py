"""Configuration for Phase 2 - PICOS Set Consolidation.

Named `p2_config`, NOT `config` - phase2/'s own `config.py` (vendored Bedrock
client config) ends up on sys.path in the same process (see below), and
Python caches imported modules by bare name: a second module also named
`config` would silently shadow phase2/'s own `config.py` for every
subsequent `from config import ...` anywhere in the process, including
inside phase2/'s own files (`extraction/pico_extraction.py` needs its OWN
`config.AWS_REGION`/`BEDROCK_MODEL_ID`). This is the exact same collision
jca_phase1/jca_phase1/config.py's docstring would document and avoids the
same way - see that file for the precedent.

The EU-27 list and Member-State canonicalisation now come from `jca_phase1`
(the current Phase 1 module) rather than the retired `phase1_scoping`
package - `jca_phase1` is the package Phase 2 actually consumes output from
(see adapter.py).

UPDATE: the sibling `phase2/` folder no longer goes on sys.path (see below) -
`extraction/pico_extraction.py`'s `config` module can no longer collide with
this one. The `p2_config` name is kept anyway (no reason to churn every
`import p2_config` call site for a rationale that's now moot).
"""

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # .../PHASE1_in_our_whole_flow/phase2_pico_consolidation
_PLAN_DIR = os.path.dirname(_THIS_DIR)                            # .../PHASE1_in_our_whole_flow
_JCA_PHASE1_DIR = os.path.join(_THIS_DIR, "jca_phase1")           # .../phase2_pico_consolidation/jca_phase1 (contains the jca_phase1/ package)
# _PHASE2_RETRIEVAL_DIR = os.path.join(_PLAN_DIR, "phase2")        # DROPPED: required a sibling phase2/ folder
# (one level up from phase2_pico_consolidation/) purely to reach phase2/extraction/pico_extraction.py's
# call_bedrock(). That made this package undeployable on its own - copying phase2_pico_consolidation/
# to another machine without also copying the sibling phase2/ folder in the exact same relative layout
# raised ModuleNotFoundError: No module named 'extraction.pico_extraction' (see llm_client.py, which now
# imports call_bedrock from the local bedrock_client.py instead - no sibling folder needed).

for _p in (_JCA_PHASE1_DIR,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from jca_phase1.config import (  # noqa: E402
    EU_27_MEMBER_STATES, EU_WIDE, GENERAL_EVIDENCE, canonicalize_member_state,
)

# ---------------------------------------------------------------------------
# Static assets this package depends on. Paths are relative to the JCA
# workspace root, not hardcoded absolute - resolved via an explicit override
# env var first (production deployment), falling back to the known location
# in this development environment.
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = os.path.dirname(_PLAN_DIR)  # .../jca

HTA_GUIDANCE_PDF_PATH = os.environ.get(
    "HTA_GUIDANCE_PDF_PATH",
    # os.path.join(_WORKSPACE_ROOT, "PHASE2_JCA_PICO_CONSOLIDATION", "hta_jca_scoping-process_en.pdf"),
    # DROPPED default: same undeployable-sibling-folder problem as the old extraction/pico_extraction.py
    # import (see the note further up) - PHASE2_JCA_PICO_CONSOLIDATION/ lives two levels up, outside
    # phase2_pico_consolidation/, so it never travels when this folder is copied/zipped on its own
    # ("HTA-JCA guidance PDF not found" on a teammate's machine). A copy of the PDF already lives at
    # _THIS_DIR - resolve the default from there instead so the package is self-contained.
    os.path.join(_THIS_DIR, "hta_jca_scoping-process_en.pdf"),
)

# Section 3.2 "PICO consolidation" spans exactly these pages in the actual
# PDF (confirmed by direct inspection - see PHASE2_MINIMAL_CHANGE_
# IMPLEMENTATION_PLAN.md Section 16F). Page 27 begins Section 3.3 and is
# deliberately excluded - it is not part of the consolidation methodology.
HTA_GUIDANCE_SECTION_3_2_PAGES = (19, 26)  # inclusive, 1-indexed

PROMPT_STORE_DIR = os.environ.get(
    "PHASE2_PROMPT_STORE_DIR",
    os.path.join(_THIS_DIR, "prompts", "_store"),
)

RESULTS_DIR = os.environ.get(
    "PHASE2_RESULTS_DIR",
    os.path.join(_THIS_DIR, "results"),
)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")  # populated from phase2/.env at import of call_bedrock's module

# Max tokens per agent call - Agent 2 (consolidation) reasons over the most
# data (every Member State's requirement for a population) and gets the
# largest budget; Agent 1/4 checks are comparatively small.
#
# MAX_TOKENS_PICO_CONSOLIDATION was 8000 - a real run against a 9-comparator
# population (Tarlatamab, ES-SCLC; ~27 Member States each) silently
# exceeded it on every attempt: the response was cut off mid-JSON
# (stop_reason=max_tokens), which fails schema parsing exactly like a
# genuinely malformed response and blocked both of that population's
# licensed/intended_to_treat runs. The one-shot retry in llm_client.
# invoke_structured() re-sends the same max_tokens, so it does not recover
# from this case - only a larger budget does. Bumped well above the largest
# real payload seen so far; llm_client.py now also reports truncation
# explicitly if this ever needs raising again.
MAX_TOKENS_CONTEXT_LOCKING = 1500
MAX_TOKENS_PICO_CONSOLIDATION = 16000
MAX_TOKENS_RATIONALE = 1000
MAX_TOKENS_SEMANTIC_VALIDATION = 4000

# Bounded concurrency for per-population / per-PICO-set fan-out (task
# requirement: never unbounded LLM concurrency).
MAX_CONCURRENT_POPULATIONS = 4
MAX_CONCURRENT_RATIONALE_CALLS = 6
MAX_CONCURRENT_VALIDATION_CALLS = 6

os.makedirs(PROMPT_STORE_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

__all__ = [
    "EU_27_MEMBER_STATES", "EU_WIDE", "GENERAL_EVIDENCE", "canonicalize_member_state",
    "HTA_GUIDANCE_PDF_PATH", "HTA_GUIDANCE_SECTION_3_2_PAGES", "PROMPT_STORE_DIR", "RESULTS_DIR",
    "BEDROCK_MODEL_ID", "MAX_TOKENS_CONTEXT_LOCKING", "MAX_TOKENS_PICO_CONSOLIDATION",
    "MAX_TOKENS_RATIONALE", "MAX_TOKENS_SEMANTIC_VALIDATION",
    "MAX_CONCURRENT_POPULATIONS", "MAX_CONCURRENT_RATIONALE_CALLS", "MAX_CONCURRENT_VALIDATION_CALLS",
]
