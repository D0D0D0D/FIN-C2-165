"""Integration — 3-mode E2E, tenant isolation, and provenance fail-closed."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import TradeFinanceLCPreflightAgent
from src.scope import scope_for
from src.services import wire_services

_REF_KEYS = {"preflight": "plan_draft_ref", "ingest": "ingest_payload_ref", "feedback": "feedback_payload_ref"}


def _invoke(services, *, mode, payload, case_ref="", tenant="tenant-a", session="sess-a", caller_id="lc-officer"):
    scope = scope_for(tenant)
    store = services["payload_store"]
    ref = store.put(payload, scope=scope, session_id=session)
    envelope = json.dumps({"mode": mode, "case_ref": case_ref, "scope": {"tenant_id": tenant}, _REF_KEYS[mode]: ref})
    graph = TradeFinanceLCPreflightAgent(
        {
            "services": services,
            "authorized_approvers": ["lc-officer"],
            "trusted_scope": scope,
            "session_id": session,
        }
    )
    graph.compile()
    # caller_id is the verified approver identity used by FeedbackNode;
    # it is ignored by preflight/ingest but required for KB promotion.
    return graph.invoke(
        envelope,
        ctx=InvocationContext(session_id=session, caller_id=caller_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )


def _new_graph():
    services = wire_services()
    return services


def test_ingest_preflight_feedback_workflow_and_tenant_isolation():
    services = _new_graph()

    ingest = _invoke(
        services,
        mode="ingest",
        payload=[
            {
                "record_id": "rp-1",
                "record_type": "review_point",
                "observed_at": "2026-01-01",
                "review_point": "invoice amount mismatch vs L/C",
                "inquiry_reason": "amount tolerance exceeded",
            }
        ],
    )
    assert ingest["status"] == "success"
    assert ingest["formatted_output"]["ingest_summary"]["written_count"] == 1

    payload = {
        "lc": {"required_docs": ["invoice", "bill_of_lading"], "amount": 10000, "tolerance_pct": 5, "presentation_period_days": 21, "expiry": "2026-03-01"},
        "documents": {"invoice": {"amount": 10600}, "bill_of_lading": {"on_board_date": "2026-02-01"}},
        "as_of_date": "2026-02-10",
    }
    preflight = _invoke(services, mode="preflight", payload=payload, case_ref="case-1")
    assert preflight["status"] == "success"
    fo = preflight["formatted_output"]
    assert set(fo) >= {
        "facts",
        "inconsistency_candidates",
        "bank_inquiry_points",
        "correction_suggestions",
        "evidence_map",
        "requires_human_review",
    }
    assert fo["facts"]["deadline_status"] == "on_time"
    # amount out of tolerance surfaces as an inconsistency candidate
    assert any("amount" in c["description"].lower() for c in fo["inconsistency_candidates"])

    # tenant isolation: another tenant recalls nothing from tenant-a's KB
    other = _invoke(services, mode="preflight", payload=payload, case_ref="case-2", tenant="tenant-b")
    assert other["status"] == "success"
    assert other["formatted_output"]["bank_inquiry_points"] == []

    feedback = _invoke(
        services,
        mode="feedback",
        payload={"case_ref": "case-1", "event_type": "kb_promoted", "approved_by": "lc-officer", "source_id": "rp-1", "event_id": "evt-1"},
    )
    assert feedback["status"] == "success"
    assert feedback["formatted_output"]["feedback_result"]["kb_updated"] is True


def test_preflight_output_never_carries_verdict_fields():
    services = _new_graph()
    payload = {
        "lc": {"required_docs": ["invoice"], "amount": 100, "tolerance_pct": 0},
        "documents": {"invoice": {"amount": 100}, "bill_of_lading": {"on_board_date": "2026-02-01"}},
        "as_of_date": "2026-02-05",
    }
    out = _invoke(services, mode="preflight", payload=payload, case_ref="c")
    text = json.dumps(out["formatted_output"]).casefold()
    for banned in ("payment_verdict", "presentation_approved", "authenticity_verdict", "discrepancy_final"):
        assert banned not in text


def test_missing_scope_fails_closed():
    services = _new_graph()
    store = services["payload_store"]
    # missing tenant scope in the envelope -> status=error (pre_process refuses;
    # the backbone routes an error status to finalize, so no crash and no brief).
    ref = store.put({"lc": {}, "documents": {}}, scope=scope_for("tenant-a"), session_id="s")
    envelope = json.dumps({"mode": "preflight", "case_ref": "c", "scope": {}, "plan_draft_ref": ref})
    graph = TradeFinanceLCPreflightAgent(
        {"services": services, "trusted_scope": scope_for("tenant-a"), "session_id": "s"}
    )
    graph.compile()
    result = graph.invoke(envelope, ctx=InvocationContext(session_id="s", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL))
    assert result["status"] == "error"
    assert any("scope" in msg for msg in result["error_log"])


def test_unknown_mode_fails_closed():
    services = _new_graph()
    store = services["payload_store"]
    ref = store.put({"x": 1}, scope=scope_for("tenant-a"), session_id="s")
    envelope = json.dumps({"mode": "bogus", "scope": {"tenant_id": "tenant-a"}, "plan_draft_ref": ref})
    graph = TradeFinanceLCPreflightAgent(
        {"services": services, "trusted_scope": scope_for("tenant-a"), "session_id": "s"}
    )
    graph.compile()
    result = graph.invoke(envelope, ctx=InvocationContext(session_id="s", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL))
    assert result["status"] == "error"
