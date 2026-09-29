# Fix: Agent 2 (PICO Consolidation) fails with "Bedrock returned an empty response" on one population but not another

**Status:** Diagnostics fixed; underlying cause still needs a live Bedrock trace to confirm
**Files changed:** [`bedrock_client.py`](bedrock_client.py), [`llm_client.py`](llm_client.py)
**Date:** 2026-09-29

## Symptom

After [`FIX_agent2_pico_consolidation_truncated_on_large_population.md`](FIX_agent2_pico_consolidation_truncated_on_large_population.md)
was applied (`MAX_TOKENS_PICO_CONSOLIDATION` raised `8000` -> `16000`), a re-run of the same
Tarlatamab (ES-SCLC) Phase 1 output made real progress: the `licensed` population now completes
in full - 14 selected comparators, 14 PICO sets, 2 correctly blocked sets, 27-Member-State check
passing.

The `intended_to_treat` population for the *same run*, however, still failed once:

```
Agent 2 — PICO Consolidation
Cause (error type): LLMCallFailed
Message: Failed to obtain a schema-valid PicoConsolidationOutput after 2 attempt(s):
         Bedrock returned an empty response (possible content filtering or truncation).
```

## How this differs from the previous fix

The previous failure was a parse/schema failure on a non-empty but truncated response (cut off
mid-JSON by `max_tokens`). This one is different: `llm_client.py`'s
`invoke_llm_with_retry()` raises this specific message only when the Bedrock call itself
**succeeded** (no exception, nothing for `call_bedrock`'s internal 3x retry to catch) but the
parsed response body's `text` came back empty. Per `adapter.py`, `licensed` and
`intended_to_treat` share the exact same top-level `comparators`/`outcomes` (Phase 1
pre-adjudicates them across every declared population - they aren't partitioned per
population), so this is not a payload-size regression: both populations send Agent 2 an
essentially identically-sized prompt, and one produced a normal response while the other did
not.

## Root cause (diagnostics gap, not fully confirmed)

`bedrock_client.py` already reads the Anthropic response's `stop_reason` field, but previously
only collapsed it into a `truncated: bool` (`stop_reason == "max_tokens"`) - the actual value
was discarded. `llm_client.py`'s "empty response" branch could therefore only ever guess at the
cause in its message ("possible content filtering or truncation"), with no way to tell a
`max_tokens`-with-zero-output-tokens case apart from Anthropic/Bedrock's own **content-filter**
stop reason, which returns a normal (non-error) response with an empty `content` list. A
content-filter block on this specific request is the most likely explanation here, since the
call did not raise (ruling out throttling/network errors, which `call_bedrock` already retries
and would otherwise surface as `"Bedrock call failed after retries: ..."`), and the `licensed`
population's near-identical payload did not trigger it.

This was not fully confirmed against a live trace as part of this fix - see "Next steps".

## Fix (this pass)

1. **`bedrock_client.py`** - `call_bedrock()` now returns the raw `stop_reason` string as its
   own top-level key alongside the existing `truncated` bool, instead of discarding it.
2. **`llm_client.py`** - `invoke_llm_with_retry()`'s empty-response error now reports the actual
   `stop_reason` value (e.g. `stop_reason='content_filtered'`) instead of the previous generic,
   guessed message. The next time this happens - for this population or any other - the Error
   Log entry itself will say definitively whether it was a content filter, a genuine
   `max_tokens` cutoff with no output, or something else, without needing to re-run and inspect
   raw Bedrock output.

## Next steps (not done here)

- Re-run this exact population against live Bedrock and read the new `stop_reason` value off
  the resulting error message (or the raw response, if captured) to confirm whether this is
  content filtering.
- If it is confirmed as content filtering: this is a **methodology-content** problem, not a
  token-budget one - retrying with a larger `max_tokens` (as was done for the truncation fix)
  will not help, since the filter is evaluating the request/response content, not its length.
  The right fix at that point is almost certainly on the prompt/content side (e.g. checking
  whether `intended_to_treat`'s specific `indication_disease`/attributes text, or something in
  the shared comparator payload, is tripping Anthropic's safety classifier) - not another
  `llm_client.py`/`p2_config.py` change.
- If instead it turns out to be transient (e.g. a flaky `max_tokens=0` response with a normal
  `stop_reason`), the existing one-shot retry in `invoke_structured()` may need to become
  more than one attempt for this specific agent, since a single retry with identical inputs
  offers no real recovery odds against a repeatable condition.

## Verification

- Full test suite: 61 passed, 1 pre-existing unrelated failure (`test_xlsx_adapter.py`, already
  documented as pre-existing in `FIX_intended_to_treat_population_dropped.md`) - no regressions
  from this change.
- Not verified against a live Bedrock call (would require re-running the Tarlatamab pipeline
  with real credentials) - the new `stop_reason` value from that next run is what will actually
  confirm or rule out content filtering as the cause.

## Affected files

| File | Change |
|---|---|
| `bedrock_client.py` | `call_bedrock()` now returns the raw `stop_reason` string, not just the derived `truncated` bool |
| `llm_client.py` | Empty-response `LLMCallFailed` message now reports the actual `stop_reason` instead of guessing |

No other files required changes.
