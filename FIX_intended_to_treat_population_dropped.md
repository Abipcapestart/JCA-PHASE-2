# Fix: "Intended to treat" population dropped by Phase 2 adapter

**Status:** Fixed
**File changed:** [`adapter.py`](adapter.py)
**Date:** 2026-09-28

## Symptom

Phase 1 (`jca_phase1`) produces output for **two** populations per product —
`licensed` and `intended_to_treat` (ITT) — but Phase 2's final consolidated
output (Streamlit UI, Word/Excel exports) only ever showed the `licensed`
population. The "Intended to treat" section was missing entirely, not just
empty.

## Root cause

`adapter.py` is the module that converts a raw `jca_phase1` run dict into
Phase 2's internal `Phase2Input` contract. A single Phase 1 run's
`output.populations` list can contain **multiple** population structs — one
per declared population (e.g. `populations[0]` = `licensed`,
`populations[1]` = `intended_to_treat`), confirmed by the run's own
`"populations_processed": ["licensed", "intended_to_treat"]` metadata and by
`jca_phase1/ARCHITECTURE.md` ("licensed population (+ ITT population if
declared)").

The adapter's `_adapt_population()` helper, however, unconditionally read
only the first entry:

```python
# adapter.py (before fix)
pop_structs = run.get("populations") or []
pop_struct = pop_structs[0] if pop_structs else {}   # always index 0
```

`adapt_orchestrator_result()` called this helper exactly once per Phase 1
run dict, so it built exactly one `PopulationInstance` — always labeled
`"licensed"` — no matter how many population structs the run actually
contained. `intended_to_treat` was never constructed as an input, so nothing
downstream (Agent 2/3/4 consolidation, exporters, UI) ever saw it. It wasn't
being filtered out later — it never entered the pipeline in the first place.

This mismatch traced back to a stale assumption documented in the module's
old docstring: that `run_phase1()` processes exactly one population per
call, and that a caller wanting both populations must run Phase 1 twice and
feed Phase 2 two separate run files. That no longer reflects how
`jca_phase1` actually works — a single run already bundles both population
structs in one `populations[]` list.

## Fix

`adapter.py` now builds one `PopulationInstance` **per population struct**,
not per run dict:

1. `adapt_orchestrator_result()` iterates over every entry in
   `run["populations"]` for each Phase 1 run dict, calling
   `_adapt_population(run, pop_struct)` once per entry.
2. `_adapt_population()` takes the specific `pop_struct` to adapt as a
   parameter instead of reaching into `run["populations"][0]` itself.
3. `comparators`, `outcomes`, `not_identified_states`, and
   `member_state_summary` are still read from the run's **top level** (not
   from `pop_struct`) — Phase 1 doesn't partition that evidence per
   population; it pre-adjudicates it across every declared population (see
   each `scope_boundaries[].out_of_bounds_rule`, e.g. `"flag_as_itt"`). Both
   `licensed` and `intended_to_treat` `PopulationInstance`s therefore reuse
   the same underlying comparator/outcome evidence, differing in their
   `population_label`, `indication_disease`, and `attributes`.
4. The legacy path — one Phase 1 run file per population, passed as
   multiple dicts to `adapt_orchestrator_result()` — still works unchanged,
   since each such run dict simply contributes one population struct instead
   of two.
5. The module docstring was updated to describe actual current behavior
   instead of the stale one-population-per-call assumption.

Everything downstream (`graph.py`'s per-population Agent 2→3→4 fan-out,
`docx_export.py`, `excel_export.py`, the Streamlit UI's population loop) was
already written to handle a list of multiple populations correctly — the fix
was isolated to this one adapter file.

## Verification

- All existing `tests/test_adapter.py` cases (11) pass unchanged.
- Full test suite passes except two pre-existing, unrelated failures verified
  present on unmodified `main` via `git stash` (missing `pypdf` dependency in
  `test_methodology.py`; an xlsx-loader assertion in `test_xlsx_adapter.py`)
  — neither touches `adapter.py`.
- Ran the adapter directly against a real sample run
  (`RUN-20260915T130204-0de62c.json`, product Tovorafenib):

  ```
  num populations: 2
   - licensed          | comparators: 3 | outcomes: 51
   - intended_to_treat | comparators: 3 | outcomes: 51
  warnings: []
  ```

  Before the fix, this produced exactly 1 population (`licensed` only).

## Affected files

| File | Change |
|---|---|
| `adapter.py` | Core fix — iterate all population structs per run; updated docstring |

No other files required changes.
