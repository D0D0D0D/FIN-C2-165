from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from llm_draft_stage import DraftStageHooks, LlmDraftStageNode, parse_json_object


class FakeLLM:
    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return {"content": self.content}


def hooks():
    def build_prompt(state):
        return f"draft from {state.get('topic')}"

    def fallback(state):
        return {"body": f"fallback {state.get('topic')}", "cited_source_ids": ["src_1"]}

    def evidence(_state, draft):
        cited = list(draft.get("cited_source_ids", []))
        return [{"claim_id": "C-01", "claim_text": draft.get("body", ""), "source_ids": cited}], cited

    return DraftStageHooks(build_prompt=build_prompt, build_fallback=fallback, build_evidence=evidence)


def test_call_path_uses_injected_llm_from_config_and_outputs_evidence_contract():
    llm = FakeLLM('{"body":"draft","cited_source_ids":["src_1"]}')
    node = LlmDraftStageNode(config={"llm": llm}, hooks=hooks())

    result = node({"topic": "case", "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "draft"
    assert result["cited_source_ids"] == ["src_1"]
    assert result["evidence_map"][0]["source_ids"] == ["src_1"]
    assert llm.calls[0][0]["content"] == "draft from case"


def test_llm_absent_returns_deterministic_fallback_with_degradation():
    node = LlmDraftStageNode(config={}, hooks=hooks())

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "fallback case"
    assert "LLM not configured" in result["degradation_reason"]


def test_llm_parse_failure_is_fail_closed_and_does_not_emit_raw_text():
    node = LlmDraftStageNode(config={"llm": FakeLLM("not json raw draft")}, hooks=hooks())

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.ERROR.value
    assert "parse failed" in result["error_log"][0]
    assert "not json raw draft" not in str(result)


def test_parse_json_object_accepts_fenced_json_and_rejects_array():
    assert parse_json_object('```json\n{"x":1}\n```') == {"x": 1}

    try:
        parse_json_object("[1,2]")
    except ValueError as exc:
        assert "not an object" in str(exc)
    else:
        raise AssertionError("array must be rejected")
