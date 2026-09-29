"""Self-contained Bedrock (Anthropic-on-Bedrock) client for Phase 2.

Vendored locally so `phase2_pico_consolidation/` has no dependency on the
sibling `phase2/` folder (`extraction/pico_extraction.py`) - this package
must be deployable on its own (see p2_config.py's docstring for the prior
sys.path-based wiring this replaces). Mirrors the boto3 `invoke_model` call
already used by `jca_phase1/jca_phase1/providers/llm.py`'s `BedrockLLM` (same
Anthropic-on-Bedrock message format), adapted to the
`call_bedrock(system_prompt, user_prompt, max_tokens) -> dict` contract
`llm_client.py` expects: `text`/`input_tokens`/`output_tokens`/`latency_s`/
`truncated`/`stop_reason` as flat top-level keys (llm_client.py's
`invoke_llm_with_retry` reads them exactly this way).

`stop_reason` is passed through verbatim (not just collapsed into the
`truncated` bool) so a genuinely empty completion - `content` present but
with no text, e.g. Anthropic's own content-filter stop reason - can be
told apart from a `max_tokens` cutoff in whatever raises on it
(llm_client.py's "empty response" check previously could only guess at the
cause in its message; see FIX_agent2_pico_consolidation_empty_response.md).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_THIS_DIR, ".env"))
except ImportError:
    pass

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 3.0

_client = None


def _lazy_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _client = boto3.client(
            "bedrock-runtime", region_name=AWS_REGION,
            config=BotoConfig(read_timeout=300, connect_timeout=20, retries={"max_attempts": 0}),
        )
    return _client


def call_bedrock(system_prompt: str, user_prompt: str, max_tokens: int) -> Dict[str, Any]:
    """Invoke Claude via Bedrock, retrying transient failures up to
    MAX_ATTEMPTS times with exponential backoff (matches the retry contract
    llm_client.py's docstring already assumes)."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_prompt}]}],
    }
    client = _lazy_client()

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.time()
        try:
            resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
            payload = json.loads(resp["body"].read())
            text = "".join(b.get("text", "") for b in payload.get("content", []))
            usage = payload.get("usage", {}) or {}
            stop_reason = payload.get("stop_reason")
            return {
                "text": text,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "latency_s": round(time.time() - started, 3),
                "truncated": stop_reason == "max_tokens",
                "stop_reason": stop_reason,
            }
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised after the last attempt
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise last_error  # type: ignore[misc] - loop always sets last_error before falling through
