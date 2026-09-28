"""Test bootstrap — expose repo `src/` on sys.path.

Parts are applied (copied) into `repo/src/<part_module>/`. Their bundled tests
under `tests/parts/<part-id>/` import those modules by top-level name
(e.g. `from scoped_retrieval import ...`), so `repo/src` must be importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.graph.graph import TradeFinanceLCPreflightAgent  # noqa: E402
from src.scope import scope_for  # noqa: E402
from src.services import wire_services  # noqa: E402


_LIVE_SCOPE = scope_for("tenant-live")
_LIVE_SESSION_ID = "live-1"
_LIVE_CASE_REF = "livecase"


def _live_preflight_payload() -> dict:
    """Exercise deterministic checks and semantic L/C/document collation."""
    return {
        "lc": {
            "term_id": "lc-term-live",
            "required_docs": [
                "commercial_invoice",
                "packing_list",
                "bill_of_lading",
                "insurance_certificate",
            ],
            "amount": 100_000,
            "quantity": 1_000,
            "tolerance_pct": 5,
            "presentation_period_days": 21,
            "expiry": "2026-09-30",
            "special_conditions": (
                "The commercial invoice must state origin Japan. "
                "The bill of lading must be clean on board, and the packing list "
                "must agree with the invoice description and gross weight."
            ),
        },
        "documents": {
            "commercial_invoice": {
                "link_id": "doc-invoice-live",
                "amount": 106_000,
                "quantity": 1_000,
                "text": (
                    "Commercial invoice for industrial pumps. Country of origin: Korea. "
                    "Invoice amount USD 106000."
                ),
            },
            "packing_list": {
                "link_id": "doc-packing-live",
                "text": "Packing list for replacement pump parts; gross weight 1240 kg.",
            },
            "bill_of_lading": {
                "link_id": "doc-lading-live",
                "on_board_date": "2026-08-01",
                "text": "On-board bill of lading. Packages received with wet outer wrapping.",
            },
        },
        "as_of_date": "2026-08-12",
    }


@pytest.fixture
def _live_services_and_ref(monkeypatch):
    services = wire_services()
    services.pop("llm", None)
    ref = services["payload_store"].put(
        _live_preflight_payload(),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )

    # The live-check tool injects at config["llm"], while this older graph
    # reads the seam from config["services"]["llm"]. Bridge those two shapes
    # only for the generated pytest so production code remains untouched.
    original_init = TradeFinanceLCPreflightAgent.__init__

    def init_with_injected_llm(self, config=None):
        resolved = dict(config or {})
        configured_services = resolved.get("services")
        if isinstance(configured_services, dict) and resolved.get("llm") is not None:
            configured_services["llm"] = resolved["llm"]
        original_init(self, resolved)

    monkeypatch.setattr(TradeFinanceLCPreflightAgent, "__init__", init_with_injected_llm)
    return services, ref


@pytest.fixture
def agent_config(_live_services_and_ref) -> dict:
    services, _ = _live_services_and_ref
    return {
        "services": services,
        "trusted_scope": dict(_LIVE_SCOPE),
        "session_id": _LIVE_SESSION_ID,
        "authorized_approvers": [],
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_services_and_ref) -> dict:
    _services, ref = _live_services_and_ref
    return {
        "mode": "preflight",
        "case_ref": _LIVE_CASE_REF,
        "scope": dict(_LIVE_SCOPE),
        "plan_draft_ref": ref,
    }


@pytest.fixture
def live_allowed_degradation_reasons() -> set[str]:
    """実 LLM 1回転チェック（work/scripts/mrb_live_llm_check.py）で許容する縮退理由。

    scoped retrieval のインデックスは本 repo の統合テストが seed する。実 LLM 1回転
    # チェックは未シードで回すため、この縮退理由だけは想定内として許可する
    # （LLM 経路は実際に呼ばれ、facts / inconsistency_candidates に実質的な内容が出ている）。
    """
    return {"no scoped retrieval hits"}
