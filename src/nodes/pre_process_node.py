"""Validate the checkpoint-safe adapter envelope (tenant fail-closed / S-2)."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_REF_KEYS = {
    "preflight": "plan_draft_ref",
    "ingest": "ingest_payload_ref",
    "feedback": "feedback_payload_ref",
}


class PreProcessNode(FunctionNode):
    """Validate mode / authoritative tenant scope / payload_ref (fail-closed).

    The tenant_id comes ONLY from the adapter-injected verified scope in the
    envelope; a client-supplied tenant value is never used (design §2-3).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if all(key in state for key in ("correlation_id", "session_id", "thread_id", "trace_id")):
            InvocationContext.from_state(state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        try:
            envelope = json.loads(str(state.get("user_input", "")))
        except json.JSONDecodeError:
            envelope = None
        if not isinstance(envelope, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["invalid adapter envelope"]}

        mode = envelope.get("mode")
        scope = envelope.get("scope") if isinstance(envelope.get("scope"), dict) else {}
        tenant_id = scope.get("tenant_id")
        ref_key = _REF_KEYS.get(mode)
        if mode not in _REF_KEYS or not tenant_id or not ref_key or not envelope.get(ref_key):
            return {"status": AgentStatus.ERROR.value, "error_log": ["mode, authoritative scope, or payload_ref missing"]}

        validated = {
            "mode": mode,
            "case_ref": str(envelope.get("case_ref") or ""),
            "scope": {"tenant_id": str(tenant_id)},
            ref_key: str(envelope[ref_key]),
        }
        emit_trace_event("lc_preflight_input_validated", {"mode": mode}, state)
        return {
            "mode": mode,
            "tenant_id": str(tenant_id),
            "case_ref": str(envelope.get("case_ref") or ""),
            "validated_input": validated,
            ref_key: str(envelope[ref_key]),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
