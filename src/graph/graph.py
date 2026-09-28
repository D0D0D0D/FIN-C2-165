"""Outer Cat2 graph (fixed 5-node backbone) and the adapted main-slot GraphNode.

Design §1/§2-2: outer = AgentBaseGraph, main slot = LCPreflightGraphNode
(GraphNode) delegating to the inner DomainWorkflowGraph (BaseGraph, Stage1-4).
Non-HITL (design §6, memory_enabled: false).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services import wire_services

_REF_KEYS = {
    "preflight": "plan_draft_ref",
    "ingest": "ingest_payload_ref",
    "feedback": "feedback_payload_ref",
}


class LCPreflightGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy = "propagate"
    propagate_hitl = False
    mode_specs = {
        "ingest": ModeSpec(gate_kind="ingest", fields={"ingest_summary": "ingest_summary", "error_log": "error_log"}),
        "preflight": ModeSpec(
            gate_kind="draft",
            fields={
                "preflight_brief": "preflight_brief",
                "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "requires_human_review": "requires_human_review",
                "error_log": "error_log",
            },
        ),
        "feedback": ModeSpec(gate_kind="feedback", fields={"feedback_result": "feedback_result", "error_log": "error_log"}),
    }

    def _parent_config(self) -> dict[str, Any]:
        return {**self._config, "memory_enabled": False, "hitl": {"enabled": False}}

    def _build_subgraph(self):
        return DomainWorkflowGraph(self._parent_config())

    def extract_input(self, state: dict) -> str:
        mode = state.get("mode")
        ref_key = _REF_KEYS.get(mode, "")
        return json.dumps(
            {
                "mode": mode,
                "case_ref": state.get("case_ref", ""),
                "scope": {"tenant_id": state.get("tenant_id")},
                ref_key: state.get(ref_key),
            },
            ensure_ascii=False,
            default=str,
        )

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        return super().merge_output(state, sub_result)

    def on_subgraph_error(self, state: dict, error: Exception) -> dict:
        return {"status": AgentStatus.ERROR.value, "error_log": [f"domain workflow error: {error}"]}


class TradeFinanceLCPreflightAgent(AgentBaseGraph):
    def __init__(self, config: dict | None = None) -> None:
        config = {"max_retry": 3, "memory_enabled": False, "hitl": {"enabled": False}, **(config or {})}
        config.setdefault("services", wire_services())
        super().__init__(config)

    @property
    def name(self) -> str:
        return "FIN-C2-165"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = LCPreflightGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: dict) -> dict:
        return {
            "formatted_output": state.get("formatted_output", {}),
            "status": state.get("status"),
            "error_log": state.get("error_log", []),
        }


Graph = TradeFinanceLCPreflightAgent
