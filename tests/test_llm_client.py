import pytest
from pydantic import BaseModel

import llm_client


class _Schema(BaseModel):
    value: str


def test_parse_structured_output_valid_json():
    result = llm_client.parse_structured_output('{"value": "ok"}', _Schema)
    assert result.value == "ok"


def test_parse_structured_output_strips_markdown_fences():
    result = llm_client.parse_structured_output('```json\n{"value": "ok"}\n```', _Schema)
    assert result.value == "ok"


def test_case15_malformed_json_raises_llm_call_failed():
    with pytest.raises(llm_client.LLMCallFailed):
        llm_client.parse_structured_output("not json at all", _Schema)


def test_missing_required_field_raises_llm_call_failed():
    with pytest.raises(llm_client.LLMCallFailed):
        llm_client.parse_structured_output('{"other_field": "x"}', _Schema)


def test_case17_empty_response_raises(monkeypatch):
    monkeypatch.setattr(llm_client, "call_bedrock", lambda *a, **k: {"text": "", "latency_s": 0.1, "truncated": False})
    with pytest.raises(llm_client.LLMCallFailed):
        llm_client.invoke_llm_with_retry("system", "user", 100)


def test_case16_bedrock_exception_raises_llm_call_failed(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("Bedrock call failed after 3 attempts: ThrottlingException")
    monkeypatch.setattr(llm_client, "call_bedrock", _raise)
    with pytest.raises(llm_client.LLMCallFailed):
        llm_client.invoke_llm_with_retry("system", "user", 100)


def test_invoke_structured_retries_once_on_parse_failure(monkeypatch):
    calls = {"n": 0}

    def _flaky_call_bedrock(system_prompt, user_prompt, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "not valid json", "latency_s": 0.1, "truncated": False}
        return {"text": '{"value": "recovered"}', "latency_s": 0.1, "truncated": False}

    monkeypatch.setattr(llm_client, "call_bedrock", _flaky_call_bedrock)
    result = llm_client.invoke_structured("system", "user", 100, _Schema, retries=1)
    assert result.value == "recovered"
    assert calls["n"] == 2


def test_invoke_structured_bounded_retries_then_raises(monkeypatch):
    monkeypatch.setattr(llm_client, "call_bedrock",
                         lambda *a, **k: {"text": "still not json", "latency_s": 0.1, "truncated": False})
    with pytest.raises(llm_client.LLMCallFailed):
        llm_client.invoke_structured("system", "user", 100, _Schema, retries=1)
