from __future__ import annotations

import json
import logging

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from llm_draft_stage import DraftStageHooks, LlmDraftStageNode, parse_json_object


class RaisingLLM:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(self, _messages: list[dict[str, str]]) -> dict:
        raise self._exc


def make_hooks(fallback_calls: list[dict] | None = None) -> DraftStageHooks:
    def build_prompt(state: dict) -> str:
        return f"draft from {state.get('topic')}"

    def build_fallback(state: dict) -> dict:
        if fallback_calls is not None:
            fallback_calls.append(state)
        return {"body": "deterministic fallback", "cited_source_ids": []}

    return DraftStageHooks(build_prompt=build_prompt, build_fallback=build_fallback)


def test_unconfigured_llm_remains_a_successful_deterministic_degradation() -> None:
    fallback_calls: list[dict] = []
    node = LlmDraftStageNode(config={"llm": None}, hooks=make_hooks(fallback_calls))

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "deterministic fallback"
    assert "LLM not configured" in result["degradation_reason"]
    assert len(fallback_calls) == 1


def test_provider_exception_is_fail_closed_without_fallback() -> None:
    fallback_calls: list[dict] = []
    node = LlmDraftStageNode(
        config={"llm": RaisingLLM(RuntimeError("provider unavailable"))},
        hooks=make_hooks(fallback_calls),
    )

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_code"] == "llm_call_failed"
    assert result["error_type"] == "RuntimeError"
    assert "draft" not in result
    assert fallback_calls == []


def test_provider_exception_message_is_absent_from_result_state_and_trace(caplog) -> None:
    marker = "LLM-DRAFT-SENSITIVE-EXCEPTION-MARKER"
    node = LlmDraftStageNode(
        config={"llm": RaisingLLM(PermissionError(f"auth rejected: {marker}"))},
        hooks=make_hooks(),
    )
    initial_state = {
        "topic": "case",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "trace_id": "trace-1",
        "correlation_id": "correlation-1",
        "session_id": "session-1",
    }

    with caplog.at_level(logging.INFO, logger="agentcore.audit"):
        result = node(initial_state)

    checkpoint_state = {**initial_state, **result}
    assert result["status"] == AgentStatus.ERROR.value
    assert marker not in json.dumps(result, default=str)
    assert marker not in json.dumps(checkpoint_state, default=str)
    assert marker not in caplog.text
    assert "llm_call_failed" in caplog.text
    assert "PermissionError" in caplog.text


def test_response_coercion_exception_is_fail_closed_without_raw_trace(caplog) -> None:
    marker = "LLM-DRAFT-RESPONSE-COERCION-MARKER"

    class ExplodingResponse:
        def __str__(self) -> str:
            raise RuntimeError(f"response coercion failed: {marker}")

    class ReturningLLM:
        def complete(self, _messages: list[dict[str, str]]) -> ExplodingResponse:
            return ExplodingResponse()

    node = LlmDraftStageNode(config={"llm": ReturningLLM()}, hooks=make_hooks())
    state = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "trace_id": "trace-2",
    }

    with caplog.at_level(logging.INFO, logger="agentcore.audit"):
        result = node(state)

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_code"] == "llm_output_parse_failed"
    assert result["error_type"] == "RuntimeError"
    assert marker not in json.dumps({**state, **result}, default=str)
    assert marker not in caplog.text


@pytest.mark.parametrize("exc_type", [RuntimeError, PermissionError, TimeoutError])
def test_provider_exception_types_return_stable_structured_error_code(exc_type) -> None:
    node = LlmDraftStageNode(
        config={"llm": RaisingLLM(exc_type("provider-specific free text"))},
        hooks=make_hooks(),
    )

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_code"] == "llm_call_failed"
    assert result["error_type"] == exc_type.__name__
    assert result["error_log"] == [
        f"LlmDraftStageNode: llm_call_failed ({exc_type.__name__})"
    ]


def test_broken_json_remains_fail_closed_at_exported_parser_and_node() -> None:
    with pytest.raises(ValueError):
        parse_json_object('{"body":')

    class BrokenJsonLLM:
        def complete(self, _messages: list[dict[str, str]]) -> dict:
            return {"content": '{"body":'}

    node = LlmDraftStageNode(config={"llm": BrokenJsonLLM()}, hooks=make_hooks())
    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.ERROR.value
    assert "draft" not in result
