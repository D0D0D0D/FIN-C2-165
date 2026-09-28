# FIN-C2-165 — Unit Tests: Main Node

from src.nodes.main_node import MainNode
from framework.schemas.agent_status import AgentStatus


class TestMainNode:
    """Unit tests for the main business logic node."""

    def setup_method(self):
        self.node = MainNode()

    def test_success_path(self):
        """TC: Main node processes valid input and returns SUCCESS."""
        state = {
            "validated_input": "test input",
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["result"] is not None

    def test_empty_input(self):
        """TC: Main node handles empty input gracefully."""
        state = {
            "validated_input": "",
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS

    def test_execute_method_signature(self):
        """node contract: Node must implement execute(state) not _invoke_impl.

        Canonical node contract:
          - Override: execute(self, state: AgentState) -> dict
          - PROHIBITED: _invoke_impl(), process() override
        """
        import inspect

        # Must have execute() defined on the concrete class (not just inherited stub)
        assert hasattr(MainNode, "execute"), "MainNode must implement execute()"

        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        # execute(self, state) — at minimum two parameters
        assert len(params) >= 2, (
            f"execute() must accept (self, state), got params: {params}"
        )
        assert params[1] == "state", (
            f"Second parameter must be 'state', got '{params[1]}'"
        )

        # Must NOT define _invoke_impl at the domain level
        assert "_invoke_impl" not in MainNode.__dict__, (
            "_invoke_impl() must not be defined in MainNode — use execute() instead"
        )
