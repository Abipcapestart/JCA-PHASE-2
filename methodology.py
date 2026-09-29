"""Agent 1 - Context/Methodology Locking support module.

Per PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md Section 10 (Agent 1): the
actual "work" here is a deterministic structural check that the HTA-JCA
guidance's Section 3.2 (pp. 19-26) is present and covers its 4 defined
steps - NOT a full LLM re-derivation of the methodology from raw PDF text on
every run. The methodology's actual decision rules (4 comparator scenarios,
cross-state attachment, the "highlight ties" behavior) are encoded once here
as structured rule text, extracted directly from the PDF by direct human-
verified reading (see PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md Section
16F/9), and reused as shared, cached context for every downstream Agent 2/4
call in a run - never re-embedding the raw PDF into every prompt.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from pypdf import PdfReader

from p2_config import HTA_GUIDANCE_PDF_PATH, HTA_GUIDANCE_SECTION_3_2_PAGES

_REQUIRED_STEP_HEADERS = [
    ("3.2.1", "3.2.1 Step 1: List the requirements per MS"),
    ("3.2.2", "3.2.2 Step 2: Create tables per population and juxtapose MS requirements"),
    ("3.2.3", "3.2.3 Step 3: Select, per population, the required treatment(s) and assign PICO(s)"),
    ("3.2.4", "3.2.4 Step 4: Create a PICO table with the results of step 3"),
]

# Encoded directly from the actual PDF, Section 3.2 (pp. 19-26) - the
# authoritative methodology Agent 2/4 apply. See PHASE2_MINIMAL_CHANGE_
# IMPLEMENTATION_PLAN.md Section 9/16F for the source verification.
METHODOLOGY_RULE_TEXT = """HTA-JCA SCOPING GUIDANCE - SECTION 3.2 "PICO CONSOLIDATION" (pp.19-26)

Objective: translate every Member State's PICO requirement into the lowest
possible number of PICOs (one population, one intervention/combination, one
comparator [which can itself include more than one treatment], at least one
outcome).

FOUR COMPARATOR SCENARIOS (Figure 3, p.17) - there is NO fifth top-level
scenario:
1. UNIQUE COMPARATOR - one treatment suitable for all patients in the
   population. -> 1 PICO, comparator selected directly.
2. SEVERAL COMPARATORS, EACH REQUIRED - 2..n treatments, ALL required
   (not alternatives). -> one separate PICO per treatment.
3. SEVERAL COMPARATORS, AT LEAST ONE REQUIRED - 2..n treatments, any ONE
   suffices. -> comparators combined with "OR" into one PICO. This scenario
   has two MS-stated sub-cases:
   3a. Treatment(s) MAY be dropped during consolidation (droppable):
       - if at least one of this state's acceptable options is already
         selected via step 3a/3b for this population, this state's need is
         already met - no new PICO.
       - otherwise, crosscheck this state's list against every other
         remaining at-least-one state's list for the SAME population; select
         the LOWEST number of treatments that satisfies every remaining
         state. If no single preference can be determined, this MUST be
         HIGHLIGHTED (surfaced for MS/assessor discussion) rather than
         silently resolved by an automatic rule - the guidance does not
         define an automatic tie-break of its own.
   3b. Treatment(s) must NOT be dropped (must-retain): the full list is
       retained as one OR-joined PICO. This only merges with another
       state's list on an EXACT match (identical set) - any difference
       means they do NOT merge; each stands separately.
4. INDIVIDUALISED TREATMENT - one comparator bundling several patient-
   specific treatment options (used when no single population-wide
   treatment exists and the population cannot be meaningfully split into
   subpopulations). -> 1 PICO per bundle. If several states each request an
   individualised comparator for the same population, the assessor/co-
   assessor and MS should explore aligning the bundle's components; this
   consolidation is only valid when components are an EXACT match after
   that alignment attempt - if they genuinely differ, separate PICOs remain.

CROSS-STATE ATTACHMENT (illustrated by the guidance's own worked example,
Tables 1-9, pp.20-25): once a comparator is selected for any reason above,
every Member State whose OWN per-state requirement lists that exact
comparator as an acceptable option is attached to that comparator's Member
State coverage - regardless of which scenario originally forced the
comparator into existence. A state's scenario only governs whether its own
need forces a NEW comparator to exist, never which states can subsequently
share an already-selected comparator's PICO.

FOUR-STEP PROCESS (Figure 4, p.26):
Step 1 - List requirements per MS: for each MS, list required population(s)
and treatment(s); classify each as unique / each-required / at-least-one
(note droppable vs must-retain) / individualised.
Step 2 - Juxtapose MS requirements per population: set apart each required
population in its own table; columns = every MS requiring that population.
Step 3 - Select treatment(s): apply the 4 scenarios above to select the
lowest number of treatments/comparators satisfying every MS's requirement.
Step 4 - Create the PICO table: each selected comparator (or OR-combination)
becomes one PICO column; all outcomes are added to every PICO.

The whole process is meant to be transparent and auditable to every MS -
an unresolved tie is surfaced, not hidden behind an unexplained automatic
choice.
"""


@dataclass
class MethodologyContext:
    methodology_loaded: bool
    guidance_version: str
    sections_covered: List[str]
    rule_text: str
    source_path: str


_cache: Optional[MethodologyContext] = None


def load_methodology(force_reload: bool = False) -> MethodologyContext:
    """Loads and caches the methodology context. Deterministic: checks the
    4 expected step headers are present in the PDF's own text layer for
    pp.19-26 (methodology_loaded), and best-effort extracts a version string
    from the document (guidance_version) - see PHASE2_MINIMAL_CHANGE_
    IMPLEMENTATION_PLAN.md Section 15 Open Question 8: the version string's
    exact expected format/location is not confirmed, so this degrades to
    "unknown" rather than guessing."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    if not os.path.exists(HTA_GUIDANCE_PDF_PATH):
        raise FileNotFoundError(
            f"HTA-JCA guidance PDF not found at {HTA_GUIDANCE_PDF_PATH!r} - "
            f"Agent 1 cannot lock methodology without this static asset.")

    reader = PdfReader(HTA_GUIDANCE_PDF_PATH)
    start, end = HTA_GUIDANCE_SECTION_3_2_PAGES
    page_texts = []
    for page_no in range(start, end + 1):
        try:
            page_texts.append(reader.pages[page_no - 1].extract_text() or "")
        except IndexError:
            break
    combined_text = "\n".join(page_texts)

    sections_covered = [name for header, name in _REQUIRED_STEP_HEADERS if header in combined_text]
    methodology_loaded = len(sections_covered) == len(_REQUIRED_STEP_HEADERS)

    guidance_version = _best_effort_version(reader)

    _cache = MethodologyContext(
        methodology_loaded=methodology_loaded,
        guidance_version=guidance_version,
        sections_covered=sections_covered,
        rule_text=METHODOLOGY_RULE_TEXT,
        source_path=HTA_GUIDANCE_PDF_PATH,
    )
    return _cache


def _best_effort_version(reader: PdfReader) -> str:
    try:
        first_page_text = reader.pages[0].extract_text() or ""
    except Exception:  # noqa: BLE001
        first_page_text = ""
    match = re.search(r"V\d+(\.\d+)?[,]?\s*\d{1,2}\s+\w+\s+\d{4}", first_page_text)
    if match:
        return match.group(0)
    meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    return meta_title or "unknown"
