# Fix: Agent 2 (PICO Consolidation) fails with "schema-valid ... after 2 attempts" on large populations

**Status:** Fixed
**Files changed:** [`p2_config.py`](p2_config.py), [`llm_client.py`](llm_client.py)
**Date:** 2026-09-29

## Symptom

Running the Streamlit UI against a Phase 1 run for Tarlatamab (ES-SCLC) produced 2 agent
failures, one for each population (`licensed` and `intended_to_treat`):

```
Agent 2 — PICO Consolidation
Cause (error type): LLMCallFailed
Message: Failed to obtain a schema-valid PicoConsolidationOutput after 2 attempts: ...
```

Both populations for that run ended up dropped entirely (`population_count = 0`,
`overall_status = BLOCKED` in the exported Excel workbook) - Agent 2 never produced usable
output for either one, so nothing downstream (Agent 3/4, Word/Excel export) had anything to
consolidate.

## Root cause

`agent2_pico_consolidation.py` sends the population's *entire* comparator list - every
comparator, every one of its per-Member-State rows - to the LLM in one call, and expects one
JSON response back within a fixed `max_tokens` budget (`MAX_TOKENS_PICO_CONSOLIDATION`,
`p2_config.py`).

The Tarlatamab run's `comparators`/`outcomes` (shared across both populations - see
[`FIX_intended_to_treat_population_dropped.md`](FIX_intended_to_treat_population_dropped.md))
had **9 comparators × up to 27 Member States = 243 per-state rows** - by far the largest
payload Agent 2 has had to consolidate; every other saved run/test fixture has 1-3 comparators.
The old budget, `8000` tokens, was sized against those smaller cases and was never revisited
against a population this large.

With that many comparators, the model's `selected_comparators[]` response (member states,
tiers, tie-break reasoning, etc. per comparator) ran past `8000` tokens and Bedrock cut the
response off mid-JSON (`stop_reason=max_tokens`). A response truncated mid-object fails
`llm_client.py`'s JSON-extraction/schema-validation step **exactly the same way** a genuinely
malformed response would - there was no signal anywhere distinguishing "the model produced
garbage" from "the model was cut off". `bedrock_client.py` already computed this
(`truncated: payload.get("stop_reason") == "max_tokens"`) but `llm_client.py` discarded the
flag instead of using it.

This also explains why it failed on *every* attempt: `invoke_structured()`'s one-shot retry
re-sends the identical `system_prompt`/`user_prompt`/`max_tokens` - a retry only helps a
one-off/transient bad response, not a response that is deterministically too large for the
budget it was given. Both populations shared the same oversized comparator list, so both
independently exhausted their attempt + retry and were logged as separate failures.

## Fix

1. **`p2_config.py`** - raised `MAX_TOKENS_PICO_CONSOLIDATION` from `8000` to `16000`, well
   above the largest real payload seen so far, so a 9+ comparator population's consolidation
   output actually fits in one response.
2. **`llm_client.py`** - `parse_structured_output()` now takes the `truncated` flag (and the
   `max_tokens` it was cut off at) from the Bedrock response, and `invoke_structured()` passes
   them through. If a parse/schema failure happens on a truncated response, the raised
   `LLMCallFailed` now says so explicitly (e.g. *"Response was truncated at max_tokens=16000
   before the JSON object could be completed - increase max_tokens for this call"*) instead of
   the previous generic "could not be parsed as JSON" / "did not satisfy schema" message, which
   showed only the first 500 characters of a truncated response - characters that look like
   perfectly valid JSON and give no hint that the real problem is at the *end*, not the start.

This makes the failure mode self-diagnosing directly from the UI's Error Log (or the
`RunLogEntry.message` in the exported JSON/Excel) if a population large enough to exceed even
the new `16000`-token budget is ever encountered again - no need to re-run and inspect raw
Bedrock output to figure out what happened, as was necessary this time.

## Verification

- Full test suite: 61 passed, 1 pre-existing unrelated failure (`test_xlsx_adapter.py`'s
  xlsx-loader assertion, already documented as pre-existing in
  `FIX_intended_to_treat_population_dropped.md`) - no regressions from this change.
- Root cause was confirmed by inspecting the actual Phase 1 output backing the failing run
  (`jca_phase1/scripts/gt_eval_output/Tarlatamab.json`): 9 comparators, 243 per-Member-State
  rows, versus 1-3 comparators in every other saved run/test fixture.
- Re-running the Tarlatamab pipeline end-to-end against live Bedrock to confirm it now
  completes was **not** done as part of this fix - it requires live AWS credentials, which
  weren't exercised here. Recommended as the next step to fully close this out.

## Affected files

| File | Change |
|---|---|
| `p2_config.py` | `MAX_TOKENS_PICO_CONSOLIDATION` raised `8000` -> `16000`, with rationale comment |
| `llm_client.py` | `parse_structured_output`/`invoke_structured` now detect and clearly report `max_tokens` truncation instead of masking it as a generic parse/schema failure |

No other files required changes.
