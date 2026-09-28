# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# CONDITIONAL: Required only when config/agent.yaml has hitl.enabled: true
# Templates that do not use HITL will see this file produce SKIPPED results.
#
# Replace MyMainNode (and the state dict) with the node that calls interrupt().
#
# Two PB-7 test cases:
#
#   test_pb7_hitl_interrupt_propagates()
#       Verifies that interrupt() raises GraphInterrupt and the signal propagates
#       through BaseNode.__call__() to the LangGraph engine — NOT caught by the
#       application error boundary.
#
#   test_pb7_hitl_allowed_false_skips_interrupt()
#       Verifies that the hitl_allowed=False guard prevents interrupt() from
#       firing — no GraphInterrupt raised, no deadlock.
#
from __future__ import annotations

import warnings
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Conditional skip — only runs when config/agent.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in project dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

# ---------------------------------------------------------------------------
# Imports (uncomment and replace MyMainNode with the template node that calls
# interrupt() inside execute())
# ---------------------------------------------------------------------------

# from src.nodes.main_node import MyMainNode  # TODO: replace with actual node class


# ---------------------------------------------------------------------------
# Helper — build a minimal base state for the node under test
# ---------------------------------------------------------------------------

def _base_state(**overrides) -> dict:
    """Return a minimal state dict for PB-7 tests.

    Replace / extend with the fields your node's execute() actually reads.
    """
    state = {
        # Framework-managed fields
        "caller_trust_level": "anonymous",
        "correlation_id": "pb7-test",
        "node_history": [],
        "error_log": [],
        # HITL fields
        "hitl_allowed": True,           # overridden per test case
        "hitl_count": 0,
        # TODO: add domain-specific fields required by execute()
        # e.g. "validated_input": "test value",
        #      "confidence_score": 0.5,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# PB-7-A: interrupt() raises GraphInterrupt and propagates
# ---------------------------------------------------------------------------


def test_pb7_hitl_interrupt_propagates() -> None:
    """PB-7: interrupt() raises GraphInterrupt and propagates (not caught by app boundary).

    Covers the first PB-7 assertion:
      GraphInterrupt reaches LangGraph engine; status is NOT set to error.

    Instructions:
      1. Uncomment the import above and set the node class.
      2. Set state fields so execute() reaches the interrupt() call
         (e.g. confidence_score below threshold, or draft ready for review).
      3. Run: python -m pytest tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py -v
    """
    pytest.skip(
        "TODO: uncomment import + replace MyMainNode; set state so execute() "
        "reaches interrupt() — then remove this pytest.skip()"
    )

    # --- Template (replace MyMainNode and state fields) ---
    # from src.nodes.main_node import MyMainNode
    # from langgraph.errors import GraphInterrupt
    #
    # node = MyMainNode()
    # state = _base_state(
    #     hitl_allowed=True,
    #     confidence_score=0.5,   # below threshold → triggers interrupt()
    # )
    # with pytest.raises(GraphInterrupt):
    #     node(state)             # call via __call__(), not execute() directly


# ---------------------------------------------------------------------------
# PB-7-B: hitl_allowed=False guard prevents deadlock
# ---------------------------------------------------------------------------


def test_pb7_hitl_allowed_false_skips_interrupt() -> None:
    """PB-7 guard: hitl_allowed=False must NOT raise GraphInterrupt (no deadlock).

    Covers the HITL guard behavior:
      When hitl_allowed=False, the node must check the flag before calling
      interrupt() and skip the HITL path entirely.

    Instructions:
      1. Uncomment the import above and set the node class.
      2. Set the same trigger condition as test_pb7_hitl_interrupt_propagates
         BUT with hitl_allowed=False — the node must NOT raise GraphInterrupt.
      3. Assert the result contains an expected field (e.g. "result" is not None).
    """
    pytest.skip(
        "TODO: uncomment import + replace MyMainNode; set state so execute() "
        "would hit interrupt() path but hitl_allowed=False suppresses it — "
        "then remove this pytest.skip()"
    )

    # --- Template (replace MyMainNode and state fields) ---
    # from src.nodes.main_node import MyMainNode
    # from langgraph.errors import GraphInterrupt
    #
    # node = MyMainNode()
    # state = _base_state(
    #     hitl_allowed=False,
    #     confidence_score=0.5,   # same trigger condition as PB-7-A
    # )
    # result = node(state)        # must NOT raise GraphInterrupt
    # assert result.get("result") is not None
