"""Legacy unit-testable main node; production uses ``LCPreflightGraphNode``.

The production `main` slot is the GraphNode in ``src/graph/graph.py``. This
thin node is retained for the scaffold unit test and the PB-6 invoke-order
scan over ``src/nodes/``.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class MainNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        emit_trace_event("legacy_main_checked", {}, state)
        return {"result": state.get("validated_input", ""), "status": AgentStatus.SUCCESS.value}
