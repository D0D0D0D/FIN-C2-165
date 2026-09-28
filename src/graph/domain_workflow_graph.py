"""Inner custom topology (BaseGraph) for preflight / ingest / feedback modes.

Design §2-3 / §2-4:
  preflight : hydrate → stage1_deterministic → stage2_recall → stage3_apply → stage4_verify → END
  ingest    : hydrate → ingest_rows → END
  feedback  : hydrate → feedback → END

Stage1 produces immutable authoritative facts; Stage3 (LLM) does semantic
collation only; Stage4 (evidence-gate) verifies claim→source and tenant scope.
"""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.domain_nodes import (
    FeedbackNode,
    HydrateEnvelopeNode,
    IngestRowsNode,
    LCRetrievalNode,
    Stage1DeterministicNode,
    Stage3ApplyNode,
    Stage4VerifyNode,
    normalize_review_query,
)
from src.scope import SCOPE_KEYS
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        services = self.config["services"]
        self._payload_store = services["payload_store"]
        self._backend = services["kb_backend"]
        self._llm_config = {"llm": services.get("llm")}
        # L/C-officer allowlist for KB promotion (config; empty = fail-closed).
        self._authorized_approvers = self.config.get("authorized_approvers", [])
        self._trusted_scope = self.config.get("trusted_scope")
        self._session_id = self.config.get("session_id")

    @property
    def name(self) -> str:
        return "lc_preflight_domain_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        if not self.config.get("services"):
            raise ValueError("services configuration is required")
        if not isinstance(self._trusted_scope, dict) or set(self._trusted_scope) != set(SCOPE_KEYS):
            raise ValueError("trusted_scope must be supplied per request")
        if not isinstance(self._session_id, str) or not self._session_id:
            raise ValueError("session_id must be supplied per request")

    def register_nodes(self) -> None:
        self._nodes = {
            "hydrate_envelope": HydrateEnvelopeNode(),
            "stage1_deterministic": Stage1DeterministicNode(self._payload_store, self._backend),
            "stage2_recall": LCRetrievalNode(
                scope_keys=SCOPE_KEYS,
                backend=self._backend,
                normalize_query=normalize_review_query,
                top_k=5,
            ),
            "stage3_apply": Stage3ApplyNode(self._llm_config, self._payload_store),
            "stage4_verify": Stage4VerifyNode(),
            "ingest_rows": IngestRowsNode(
                self._payload_store,
                self._backend,
                trusted_scope=self._trusted_scope,
                session_id=self._session_id,
            ),
            "feedback": FeedbackNode(self._payload_store, self._backend, self._authorized_approvers),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "hydrate_envelope")
        self._sg.add_conditional_edges("hydrate_envelope", self._entry_route)
        self._sg.add_edge("stage1_deterministic", "stage2_recall")
        self._sg.add_edge("stage2_recall", "stage3_apply")
        self._sg.add_edge("stage3_apply", "stage4_verify")
        self._sg.add_edge("stage4_verify", END)
        self._sg.add_edge("ingest_rows", END)
        self._sg.add_edge("feedback", END)

    def _entry_route(self, state: State) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        mode = state.get("mode")
        if mode == "preflight":
            return "stage1_deterministic"
        if mode == "ingest":
            return "ingest_rows"
        if mode == "feedback":
            return "feedback"
        return END

    def route(self, state: State) -> str:
        return END

    def get_output(self, state: State) -> dict:
        mode = state.get("mode")
        if mode == "preflight":
            return {
                key: state.get(key)
                for key in (
                    "preflight_brief",
                    "evidence_map",
                    "cited_source_ids",
                    "retrieved_records",
                    "requires_human_review",
                    "status",
                    "degradation_reason",
                    "error_log",
                )
                if key in state
            }
        if mode == "ingest":
            return {key: state.get(key) for key in ("ingest_summary", "status", "error_log") if key in state}
        return {key: state.get(key) for key in ("feedback_result", "status", "error_log") if key in state}
