"""Reusable LLM invocation/parsing helpers for Phase 2's agents.

Wraps `call_bedrock()` (bedrock_client.py, in this same package - vendored
locally so this package has no dependency on the sibling phase2/ folder;
originally phase2/extraction/pico_extraction.py, see p2_config.py's docstring
for why that sibling-folder wiring was dropped): 3-attempt exponential
backoff, already handles transient network/throttling errors. This module
adds the one missing reusable layer Phase 1 doesn't have today - a shared
fail-closed JSON-parse + Pydantic-validate helper, instead of every module
hand-rolling its own regex/json.loads pair (as phase1_scoping's harmonize.py/
structuring_agent.py/validation_agent.py all currently do, independently).

Task requirement (Section 19/28): reuse existing Bedrock resilience, never
reintroduce the previously-fixed `stopReason=content_filtered` + invalid-
prefill-retry bug. `call_bedrock()` never sends an assistant-role prefill
message (single user message only), so that failure mode does not apply
here; a filtered response instead surfaces as an empty/parseable-but-empty
`text`, handled by `LLMCallFailed` below like any other unusable response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

import p2_config  # noqa: F401 - side-effecting import: wires jca_phase1/'s dir onto sys.path
# from extraction.pico_extraction import call_bedrock  # phase2's shared Bedrock client, vendored via p2_config.py's sys.path
# DROPPED: required a sibling phase2/ folder one level up, which broke on any machine where
# phase2_pico_consolidation/ was copied on its own (ModuleNotFoundError: No module named
# 'extraction.pico_extraction'). Replaced with a self-contained client in this package instead.
from bedrock_client import call_bedrock

T = TypeVar("T", bound=BaseModel)


class LLMCallFailed(Exception):
    """Raised when an LLM call could not produce a usable, schema-valid
    response after retries. Callers MUST turn this into a blocked/flagged
    state for the specific affected item - never a fabricated result
    (task Section 18/31, no-hallucination policy)."""


@dataclass
class LLMInvocationResult:
    parsed: Any
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    truncated: bool


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """Fail-closed JSON extraction - same convention as phase1_scoping's
    existing modules (harmonize.py, structuring_agent.py, validation_agent.py):
    strip markdown fences, take the first {...} block, return None (never
    guess) on any parse failure."""
    cleaned = re.sub(r"```json|```", "", raw_text or "").strip()
    match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def invoke_llm_with_retry(system_prompt: str, user_prompt: str, max_tokens: int) -> LLMInvocationResult:
    """One logical LLM call. `call_bedrock` already retries transient
    failures 3x internally; this wrapper's job is only to normalize the
    result shape and raise `LLMCallFailed` on a genuinely empty response
    (e.g. content filtering) rather than letting an empty string flow
    downstream silently."""
    try:
        result = call_bedrock(system_prompt, user_prompt, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - call_bedrock already retried; this is a terminal failure
        raise LLMCallFailed(f"Bedrock call failed after retries: {exc}") from exc

    text = result.get("text", "")
    if not text or not text.strip():
        stop_reason = result.get("stop_reason")
        raise LLMCallFailed(
            f"Bedrock returned an empty response - stop_reason={stop_reason!r} "
            f"(a real call succeeded but produced no text content; 'content_filtered' means "
            f"Anthropic/Bedrock's safety filter blocked this specific request - see "
            f"FIX_agent2_pico_consolidation_empty_response.md).")

    # NOTE: call_bedrock() (phase2/extraction/pico_extraction.py) returns
    # input_tokens/output_tokens as flat top-level keys, not nested under a
    # "usage" sub-dict - matched exactly here, not guessed.
    return LLMInvocationResult(
        parsed=None,
        raw_text=text,
        input_tokens=result.get("input_tokens", 0),
        output_tokens=result.get("output_tokens", 0),
        latency_s=result.get("latency_s", 0.0),
        truncated=result.get("truncated", False),
    )


def parse_structured_output(raw_text: str, schema: Type[T], truncated: bool = False,
                             max_tokens: Optional[int] = None) -> T:
    """Fail-closed parse + Pydantic validation. Raises `LLMCallFailed` (never
    returns a partial/guessed object) if the response is not valid JSON or
    does not satisfy `schema` - callers must treat this exactly like any
    other LLM failure (block/flag the affected item, do not fabricate).

    `truncated`/`max_tokens` are surfaced from the Bedrock response only to
    make the raised message diagnosable - a response cut off mid-object by
    `max_tokens` fails this same parse/validate path with no other signal,
    and previously surfaced as an opaque "could not be parsed as JSON"
    (first 500 chars, which for a mid-object cutoff look like valid JSON and
    give no hint of the real cause - found via a real run where a
    9-comparator population's Agent 2 output silently exceeded
    MAX_TOKENS_PICO_CONSOLIDATION on every attempt, including the retry,
    since neither uses a larger budget)."""
    obj = _extract_json_object(raw_text)
    if obj is None:
        if truncated:
            raise LLMCallFailed(
                f"Response was truncated at max_tokens={max_tokens} before the JSON object could be "
                f"completed - increase max_tokens for this call. Raw (last 500 chars):\n{raw_text[-500:]}")
        raise LLMCallFailed(f"Response could not be parsed as JSON:\n{raw_text[:500]}")
    try:
        return schema.model_validate(obj)
    except ValidationError as exc:
        if truncated:
            raise LLMCallFailed(
                f"Response was truncated at max_tokens={max_tokens} and did not satisfy {schema.__name__} "
                f"(likely cut off mid-object): {exc}\n\nRaw (last 500 chars):\n{raw_text[-500:]}") from exc
        raise LLMCallFailed(f"Response did not satisfy {schema.__name__}:\n{exc}\n\nRaw:\n{raw_text[:500]}") from exc


def invoke_structured(system_prompt: str, user_prompt: str, max_tokens: int, schema: Type[T],
                       retries: int = 1) -> T:
    """Compose invoke + parse, with one same-prompt retry on a parse/
    validation failure (task Section 13/18: bounded retries, never
    unbounded). `call_bedrock` already retries transient network failures;
    this retry is specifically for "the model responded but the response
    didn't satisfy the schema" - a different failure class.

    NOTE: if the failure is a `max_tokens` truncation, retrying with the same
    `max_tokens` will almost certainly truncate again - this retry does not
    help that case (only genuinely transient/one-off malformed responses).
    The message raised on final failure now says so explicitly instead of
    looking identical to any other parse failure."""
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            result = invoke_llm_with_retry(system_prompt, user_prompt, max_tokens)
            return parse_structured_output(result.raw_text, schema, truncated=result.truncated, max_tokens=max_tokens)
        except LLMCallFailed as exc:
            last_error = exc
            continue
    raise LLMCallFailed(
        f"Failed to obtain a schema-valid {schema.__name__} after {retries + 1} attempt(s): {last_error}")
