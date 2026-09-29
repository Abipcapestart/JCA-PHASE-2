# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This is **Phase 2 — PICOS Set Consolidation** for the JCA (Joint Clinical Assessment) product.
It consumes a completed **Phase 1** run (the `jca_phase1` package, vendored under
[jca_phase1/](jca_phase1/) — see its own [jca_phase1/CLAUDE.md](jca_phase1/CLAUDE.md)) and
consolidates each population's per-Member-State comparator requirements into the smallest
defensible set of PICO sets, via a 4-agent LangGraph pipeline followed by Word/Excel/JSON export.

**Phase 1 vs Phase 2 boundary:** Phase 1 produces confirmed Comparator/Outcome scope per
Member State but deliberately emits `"pico_sets": null` — PICO-set generation is entirely this
package's job. Never add PICO-set logic to `jca_phase1/`; never re-derive Population/Intervention/
Comparator/Outcome scoping here — this package is a pure downstream consumer of Phase 1's real
output contract (`Phase1Output.to_dict()`), adapted by [adapter.py](adapter.py).

## Commands

```bash
# Run the full test suite from the phase2_pico_consolidation root
python -m pytest tests/ -v

# Run a single test file / test
python -m pytest tests/test_adapter.py -v
python -m pytest tests/test_agent2_scenarios.py::TestClassName::test_name -v

# Run the end-to-end CLI pipeline against one or more saved Phase 1 run JSONs
# (repeat --phase1-output once per population, e.g. licensed + intended-to-treat)
python run_pipeline.py --phase1-output path/to/phase1_run.json

# Run the Streamlit UI (from this directory; PYTHONPATH must reach the real
# jca_phase1 package — see RUNNING_UI.md for the full explanation/workaround)
PYTHONPATH="../jca_phase1" .venv/Scripts/streamlit run ui/app.py   # Git Bash
$env:PYTHONPATH = "..\jca_phase1"; .venv\Scripts\streamlit run ui\app.py   # PowerShell
```

Credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `BEDROCK_MODEL_ID`) load
from `.env` at this directory's root via `python-dotenv`, read once at process start — restart
after editing. See [RUNNING_UI.md](RUNNING_UI.md) for the full troubleshooting table.

Two pre-existing, unrelated test failures are known on an unmodified checkout: a missing `pypdf`
dependency path in `test_methodology.py` and an xlsx-loader assertion in `test_xlsx_adapter.py`
(see [FIX_intended_to_treat_population_dropped.md](FIX_intended_to_treat_population_dropped.md)
for how these were verified as pre-existing, not regressions).

## Architecture

### Pipeline shape (graph.py)

```
adapter.py (adapt_orchestrator_result)   — Phase1Output(s) -> Phase2Input, deterministic
        |
        v
context_locking (Agent 1)                — deterministic; loads the locked HTA methodology text
        |
        v
process_populations                      — bounded-concurrency fan-out, one worker per population
    each population:  Agent 2 -> Agent 3 -> Agent 4   (sequential; each stage can fan out its
                                                        own bounded-concurrency LLM calls internally)
        |
        v
Phase2RunResult  ->  docx_export.py / excel_export.py / raw JSON
```

Population fan-out is a plain `ThreadPoolExecutor` inside one LangGraph node rather than
LangGraph's `Send()` API (version-safety tradeoff, see graph.py's module docstring) — still a
real `StateGraph`, still independently testable per node. Every node/agent catches its own
exceptions and degrades to a logged `RunLogEntry` rather than crashing the whole run —
`Phase2RunResult.overall_status` becomes `"BLOCKED"` whenever any error-level log entry or
`failed_blocked` validation result exists.

### The four agents (agents/)

| Agent | Module | What's LLM vs deterministic |
|---|---|---|
| 1 — Context/Methodology Locking | `agent1_context_locking.py` | Fully deterministic — confirms the HTA guidance PDF's Section 3.2 loaded (4 required step headers present), no LLM call |
| 2 — PICO Consolidation | `agent2_pico_consolidation.py` | LLM: the core judgment task — resolves each comparator against the 4 comparator scenarios and any ties. Cross-state attachment that follows is **never** re-asked of the LLM — done in `cross_state_attachment.py` |
| 3 — Set Assembly | `agent3_set_assembly.py` | Deterministic crossing/attachment/ordering in Python; LLM used **only** to write each PICO set's rationale (bounded concurrency per set) |
| 4 — Validation | `agent4_validation.py` | Deterministic checks (Member State completeness, tier consistency vs. Phase 1's original records, constants, ordering, non-empty rationale) run in pure Python; a separate LLM semantic check (groundedness + methodology traceability) runs per set. A set is `failed_blocked` if **either** half fails |

This deterministic/LLM split is deliberate and enforced by design, not just convention: anything
checkable by pure logic (Member State counts, tier provenance, ordering) is never delegated to an
LLM, and LLM output is always parsed fail-closed (`llm_client.invoke_structured` /
`LLMCallFailed` — never fabricate on a parse/validation failure; block/flag the affected item
instead).

### Data flow / key modules

- [p2_config.py](p2_config.py) — env-driven config (`PHASE2_RESULTS_DIR`, `PHASE2_PROMPT_STORE_DIR`,
  `HTA_GUIDANCE_PDF_PATH`, token budgets, concurrency limits). Named `p2_config`, not `config`, to
  avoid a module-name collision with an unrelated `config.py` that can end up on `sys.path` in the
  same process — read the module docstring before renaming it back.
- [adapter.py](adapter.py) — the only place Phase 1's real output shape is read. A single Phase 1
  run's `populations` list can contain **multiple** population structs (e.g. `licensed` +
  `intended_to_treat`); `adapt_orchestrator_result()` builds one `PopulationInstance` per struct,
  across however many run dicts are passed in. `comparators`/`outcomes`/`not_identified_states` are
  read from each run's **top level**, not per-population — Phase 1 pre-adjudicates them across all
  declared populations already.
- [schemas.py](schemas.py) — three explicitly separate Pydantic layers, never mixed: (1) the input
  contract (`Phase2Input`, engineering-controlled, shaped directly from Phase 1's real schema),
  (2) agent output contracts reproduced field-for-field from the SME's fixed JSON schemas, (3) the
  final persisted `Phase2RunResult`. Note `Phase2PicoSet` (this package) vs. Phase 1's unrelated
  `pico_sets` field — same external name, deliberately different internal class, see the module
  docstring's "IMPORTANT NAMING NOTE".
- [methodology.py](methodology.py) — loads/caches the locked methodology rule text
  (`METHODOLOGY_RULE_TEXT`, encoded directly from the HTA guidance PDF's Section 3.2, pp. 19-26:
  the 4 comparator scenarios and cross-state attachment rule) plus a deterministic structural check
  that the PDF's expected step headers are present.
- [llm_client.py](llm_client.py) / [bedrock_client.py](bedrock_client.py) — self-contained Bedrock
  invocation + fail-closed JSON-parse/Pydantic-validate helper (`invoke_structured`). Vendored
  locally (no dependency on any sibling `phase2/` folder) so this package is deployable on its own.
- [prompts/registry.py](prompts/registry.py) — file-based, versioned prompt store per agent
  (draft/activate/restore/compare), seeded from the SME-verbatim `.md` files in
  `prompts/instructions/`. SMEs only ever edit `instruction_text` via the UI's prompt-tuning tab;
  the fixed output-format contract (`prompts/output_schemas.py`) is appended automatically at call
  time by [prompts/prompt_builder.py](prompts/prompt_builder.py) — no agent module builds its own
  prompt string.
- [docx_export.py](docx_export.py) / [excel_export.py](excel_export.py) — final deliverable
  (Word) and full-traceability workbook (Excel), both driven off `Phase2RunResult` +
  `Phase2Input` together.
- [xlsx_adapter.py](xlsx_adapter.py) — loads a `JCA_Phase1_RUN-*.xlsx` UI export (a reporting view
  only) as an alternate input path to the JSON adapter above.
- [ui/app.py](ui/app.py) — single-page Streamlit app: upload Phase 1 run(s), run the pipeline,
  inspect results, tune prompts, download deliverables. See [RUNNING_UI.md](RUNNING_UI.md) for the
  `PYTHONPATH`/`jca_phase1` wiring this depends on.

### Invariants worth knowing before touching agent code

- No comparator class is ever taken from the intervention's own mechanism — comes from the
  comparator's own INN (enforced upstream in Phase 1, carried through as `class_or_mechanism`).
- `retain_all_status` (droppable vs. must-retain) is only a real concept for the `at_least_one`
  comparator scenario — never populate/echo it for any other scenario (see
  `agent2_pico_consolidation._member_state_entry` / `_clear_droppable_note_where_not_applicable`).
- Exactly 27 EU Member States, always — a state appearing in a PICO set **and** in
  `not_identified_states` simultaneously is a contradiction Agent 4 catches deterministically. A
  state appearing in *multiple* PICO sets for the same population is normal and expected, not a
  bug (confirmed by the SME's own worked example).
- PICO sets are ordered broadest-first by Member State count — deterministic, checked by Agent 4.
- Tiers on a PICO set must trace back to Phase 1's original comparator tiers — never invented or
  recalculated; Agent 4 flags any tier that doesn't appear in the source data.
