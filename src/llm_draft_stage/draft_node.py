# PART: llm-draft-stage v0.2.0 (parts@01ca509)
"""LLM draft stage with injected prompt/fallback/evidence hooks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


PromptBuilder = Callable[[dict[str, Any]], str]
FallbackBuilder = Callable[[dict[str, Any]], dict[str, Any]]
EvidenceBuilder = Callable[[dict[str, Any], dict[str, Any]], tuple[list[dict[str, Any]], list[str]]]

_LLM_CALL_FAILED = "llm_call_failed"
_LLM_OUTPUT_PARSE_FAILED = "llm_output_parse_failed"
_LLM_NOT_CONFIGURED = "llm_not_configured"


class _LlmNotConfiguredError(Exception):
    """Internal signal for the sole deterministic-degradation condition.

    Degradation is deliberately limited to this one condition — an unconfigured
    injection seam, which is a legitimate design-time state. Every other failure
    (auth, config, timeout, provider runtime, response handling) is an error, so
    a caller can never mistake a broken provider for a successful degraded run.
    Do not turn this into a tuple of "degradable" exception types: widening it
    would silently restore the very defect this release fixed.
    """


@dataclass(frozen=True)
class DraftStageHooks:
    build_prompt: PromptBuilder
    build_fallback: FallbackBuilder
    build_evidence: EvidenceBuilder | None = None


class LlmDraftStageNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, hooks: DraftStageHooks) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._hooks = hooks

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        try:
            llm = self._configured_llm()
        except _LlmNotConfiguredError:
            draft = self._hooks.build_fallback(state)
            evidence_map, cited = self._evidence(state, draft)
            emit_trace_event(
                "llm_draft_stage_degraded",
                {"degradation_code": _LLM_NOT_CONFIGURED},
                state,
            )
            return {
                "draft": draft,
                "evidence_map": evidence_map,
                "cited_source_ids": cited,
                "status": AgentStatus.SUCCESS.value,
                "degradation_code": _LLM_NOT_CONFIGURED,
                "degradation_reason": "LLM not configured — deterministic fallback returned.",
            }

        try:
            prompt = self._hooks.build_prompt(state)
            response = llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:  # noqa: BLE001
            return self._safe_error_result(state, _LLM_CALL_FAILED, exc)

        try:
            content = response.get("content", "") if isinstance(response, dict) else str(response)
            draft = parse_json_object(content)
        except ValueError as exc:
            return self._safe_error_result(
                state,
                _LLM_OUTPUT_PARSE_FAILED,
                exc,
                log_label="LLM output parse failed (fail closed)",
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_error_result(
                state,
                _LLM_OUTPUT_PARSE_FAILED,
                exc,
                log_label="LLM output processing failed (fail closed)",
            )

        evidence_map, cited = self._evidence(state, draft)
        emit_trace_event("llm_draft_stage_complete", {"cited_count": len(cited)}, state)
        return {
            "draft": draft,
            "evidence_map": evidence_map,
            "cited_source_ids": cited,
            "status": AgentStatus.SUCCESS.value,
        }

    def _configured_llm(self) -> Any:
        llm = self._config.get("llm")
        if llm is None:
            raise _LlmNotConfiguredError
        return llm

    def _safe_error_result(
        self,
        state: dict,
        error_code: str,
        exc: Exception,
        *,
        log_label: str | None = None,
    ) -> dict[str, Any]:
        """Return and trace only stable metadata, never provider exception text.

        Unlike evidence IDs that have passed explicit validation, an LLM exception
        message is unvalidated free text and must not enter state or audit traces.
        """
        error_type = type(exc).__name__
        emit_trace_event(
            "llm_draft_stage_error",
            {"error_code": error_code, "error_type": error_type},
            state,
        )
        prefix = f"{log_label}: " if log_label is not None else ""
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": error_code,
            "error_type": error_type,
            "error_log": [f"LlmDraftStageNode: {prefix}{error_code} ({error_type})"],
        }

    def _evidence(self, state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        if self._hooks.build_evidence is not None:
            return self._hooks.build_evidence(state, draft)
        cited = [str(sid) for sid in draft.get("cited_source_ids", []) if sid]
        evidence = draft.get("evidence_map", [])
        return (evidence if isinstance(evidence, list) else []), cited


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON payload is not an object")
    return parsed
