"""Adversarial regressions — scope crossing, forged provenance, ingest guards."""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from src.nodes.domain_nodes import (
    FeedbackNode,
    IngestRowsNode,
    Stage4VerifyNode,
    _reconcile_candidates,
)
from src.services import InMemoryKBBackend
from src.payload_store.payload_store import PayloadStore
from src.scope import SCOPE_KEYS, scope_for


def _store() -> PayloadStore:
    return PayloadStore(scope_keys=SCOPE_KEYS)


def _put(store: PayloadStore, value, *, tenant: str = "tenant-a", session: str = "s") -> str:
    return store.put(value, scope=scope_for(tenant), session_id=session)


# ── Stage4 reconcile: Stage1 facts win and uncited claims are demoted ─────────


# Structured candidates are verified by (claim_type, target) VALUE against
# Stage1 facts — not by scanning free text (Fable adjudication).


def test_stage4_drops_field_diff_claim_that_contradicts_within_tolerance():
    facts = {"field_diff": {"amount": {"within_tolerance": True}}, "docs_present": {}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "field_diff", "target": "amount", "description": "amount off", "cited_source_ids": ["rp-1"]}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []
    assert "stage1_contradiction:amount_within_tolerance" in reasons


def test_stage4_drops_missing_doc_claim_when_doc_is_present():
    facts = {"field_diff": {}, "docs_present": {"invoice": True}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "missing_doc", "target": "invoice", "description": "missing", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []
    assert "stage1_contradiction:invoice_present" in reasons


def test_stage4_drops_deadline_claim_that_disagrees_with_stage1():
    facts = {"field_diff": {}, "docs_present": {}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "deadline", "target": "late", "description": "late", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []
    assert any("deadline_status_mismatch" in r for r in reasons)


def test_stage4_demotes_uncited_semantic_claim():
    facts = {"field_diff": {}, "docs_present": {}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "semantic", "target": "special_cond", "description": "Something looks off.", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []
    assert "uncited_semantic_claim" in reasons


def test_stage4_keeps_deterministic_claim_without_source():
    # a field_diff claim that AGREES with Stage1 (within_tolerance False) needs
    # no citation — the Stage1 fact is its source.
    facts = {"field_diff": {"amount": {"within_tolerance": False}}, "docs_present": {}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "field_diff", "target": "amount", "description": "amount differs", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert len(kept) == 1
    assert reasons == []


def test_stage4_llm_cannot_smuggle_false_deterministic_backing():
    # even if the LLM labels a claim field_diff for a within-tolerance field, it
    # is dropped — the verdict is re-derived from the fact, not trusted.
    facts = {"field_diff": {"amount": {"within_tolerance": True}}, "docs_present": {}, "deadline_status": "on_time"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "field_diff", "target": "amount", "description": "discrepancy!", "cited_source_ids": ["rp-1"]}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []


def test_stage4_replaces_llm_description_with_canonical_text():
    # On a valid claim_type/target the LLM's own wording is
    # discarded and replaced by server-authored text derived from Stage1 facts,
    # so a false statement cannot ride a valid claim_type.
    facts = {
        "field_diff": {"amount": {"within_tolerance": False, "lc": 10000, "doc": 10600}},
        "docs_present": {"invoice": True},
        "deadline_status": "on_time",
    }
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "field_diff", "target": "amount", "description": "The invoice is MISSING entirely.", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert len(kept) == 1
    assert "MISSING" not in kept[0]["description"]
    assert "10000" in kept[0]["description"] and "10600" in kept[0]["description"]


def test_stage4_deadline_claim_with_empty_target_uses_canonical_status():
    facts = {"field_diff": {}, "docs_present": {}, "deadline_status": "at_risk"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "deadline", "target": "", "description": "late!", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert len(kept) == 1
    assert "at_risk" in kept[0]["description"]
    assert "late" not in kept[0]["description"]


def test_stage4_deadline_claim_when_status_unknown_is_dropped():
    facts = {"field_diff": {}, "docs_present": {}, "deadline_status": "unknown"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "deadline", "target": "late", "description": "x", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert kept == []
    assert any("deadline_status_unknown" in r for r in reasons)


def test_stage4_deadline_target_presentation_uses_actual_status():
    # the Stage3 prompt tells the LLM to use target="presentation"; reconcile
    # must accept it and render the actual Stage1 status.
    facts = {"field_diff": {}, "docs_present": {}, "deadline_status": "late"}
    reasons: list[str] = []
    kept = _reconcile_candidates(
        [{"claim_type": "deadline", "target": "presentation", "description": "the LLM wording", "cited_source_ids": []}],
        facts,
        reasons,
        require_source=True,
    )
    assert len(kept) == 1
    assert "late" in kept[0]["description"]
    assert reasons == []


def test_ingest_rejects_payload_ref_from_another_tenant():
    store = _store()
    backend = InMemoryKBBackend()
    ref = _put(
        store,
        [{"record_id": "priv", "observed_at": "2026-01-01", "review_point": "private"}],
    )
    result = IngestRowsNode(
        store, backend, trusted_scope=scope_for("tenant-b"), session_id="s"
    ).execute(
        {"ingest_payload_ref": ref, "tenant_id": "tenant-b", "session_id": "s"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["IngestRowPipelineNode: payload_ref unresolvable"]
    assert backend.records == []


def test_ingest_rejects_prohibited_credential_content():
    store = _store()
    backend = InMemoryKBBackend()
    ref = _put(
        store,
        [{"record_id": "r", "observed_at": "2026-01-01", "review_point": "api_key=AKIA-secret leaked"}],
    )
    result = IngestRowsNode(
        store, backend, trusted_scope=scope_for("tenant-a"), session_id="s"
    ).execute(
        {"ingest_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s"}
    )
    # the row is rejected (not written), summarized with a reason.
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["ingest_summary"]["written_count"] == 0
    assert result["ingest_summary"]["rejected_count"] == 1
    assert backend.records == []


def test_payload_store_has_no_unscoped_get_api():
    # The unscoped compatibility get() was removed at part level (cross-tenant
    # disclosure risk). This node relies on scope-checked resolve() only.
    assert not hasattr(_store(), "get")


def test_ingest_writes_under_verified_tenant_scope():
    store = _store()
    backend = InMemoryKBBackend()
    ref = _put(
        store,
        [{"record_id": "rp-a", "observed_at": "2026-01-01", "review_point": "amount mismatch"}],
    )
    result = IngestRowsNode(
        store, backend, trusted_scope=scope_for("tenant-a"), session_id="s"
    ).execute(
        {"ingest_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s"}
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert backend.records[0]["metadata"]["tenant_id"] == "tenant-a"


def test_provenance_rejects_cited_record_from_another_tenant():
    result = {
        "gate_kind": "draft",
        "status": AgentStatus.SUCCESS.value,
        "tenant_id": "tenant-a",
        "cited_source_ids": ["rec-x"],
        "evidence_map": [{"claim_id": "c1", "source_ids": ["rec-x"]}],
        "retrieved_records": [{"source_id": "rec-x", "metadata": {"tenant_id": "tenant-b"}}],
    }
    with pytest.raises(SecurityViolationError, match="provenance_scope_mismatch"):
        Stage4VerifyNode()._extra_security_gate_output(result)


def test_provenance_rejects_forged_cited_id():
    result = {
        "gate_kind": "draft",
        "status": AgentStatus.SUCCESS.value,
        "tenant_id": "tenant-a",
        "cited_source_ids": ["forged-id"],
        "evidence_map": [{"claim_id": "c1", "source_ids": ["forged-id"]}],
        "retrieved_records": [{"source_id": "rec-real", "metadata": {"tenant_id": "tenant-a"}}],
    }
    with pytest.raises(SecurityViolationError, match="non-existent"):
        Stage4VerifyNode()._extra_security_gate_output(result)


def _feedback_backend():
    backend = InMemoryKBBackend()
    backend.register_case(tenant_id="tenant-a", case_ref="c1")
    backend.upsert(
        {"source_id": "rp-1", "title": "review_point", "summary": "x", "metadata": {"tenant_id": "tenant-a"}}
    )
    return backend


def test_feedback_kb_writeback_requires_explicit_promotion():
    store = _store()
    backend = _feedback_backend()
    # point_confirmed alone must NOT flip kb_promoted.
    ref = _put(
        store,
        {"case_ref": "c1", "event_type": "point_confirmed", "approved_by": "officer", "source_id": "rp-1", "event_id": "evt-a"},
    )
    # approver is the verified caller identity; point_confirmed does
    # not require the allowlist (only kb_promoted does).
    result = FeedbackNode(store, backend).execute(
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1", "caller_id": "officer"}
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["feedback_result"]["kb_updated"] is False
    assert backend.records[0]["metadata"].get("kb_promoted") is not True


def test_feedback_on_unknown_case_fails_closed():
    store = _store()
    backend = InMemoryKBBackend()  # no case registered
    ref = _put(
        store,
        {"case_ref": "does-not-exist", "event_type": "point_confirmed", "approved_by": "officer", "event_id": "evt-x"},
    )
    result = FeedbackNode(store, backend).execute(
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "does-not-exist", "caller_id": "officer"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["case_not_found"]


def test_feedback_resend_is_idempotent():
    backend = _feedback_backend()
    store = _store()

    def run():
        ref = _put(
            store,
            {"case_ref": "c1", "event_type": "point_confirmed", "approved_by": "officer", "event_id": "evt-1"},
        )
        return FeedbackNode(store, backend).execute(
            {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1", "caller_id": "officer"}
        )

    first = run()
    second = run()
    assert first["feedback_result"]["recorded_events"] == ["evt-1"]
    assert second["feedback_result"]["recorded_events"] == ["evt-1"]
    assert len(backend.audit_events) == 1  # no duplicate audit row on resend


def test_feedback_without_event_id_fails_closed():
    # event_id is required so resends stay idempotent.
    backend = _feedback_backend()
    store = _store()
    ref = _put(
        store,
        {"case_ref": "c1", "event_type": "point_confirmed", "approved_by": "officer"},
    )
    result = FeedbackNode(store, backend).execute(
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["invalid_feedback"]


def test_feedback_without_approver_fails_closed():
    # The approver is the VERIFIED caller identity, not the payload's
    # self-reported approved_by. A request with no caller_id has no
    # approver and must fail closed, even if the payload forges an approved_by.
    store = _store()
    backend = InMemoryKBBackend()
    ref = _put(
        store,
        {"case_ref": "c1", "event_type": "point_confirmed", "approved_by": "forged-officer", "event_id": "evt-z"},
    )
    result = FeedbackNode(store, backend).execute(
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["invalid_feedback"]


def test_kb_promotion_cannot_be_self_authorized_from_payload():
    """An authenticated but unlisted caller cannot promote a
    review point into the shared KB, even when the payload forges an approver
    name in approved_by. Promotion authority is the verified caller identity
    checked against the allowlist, never the payload (design §10-1)."""
    store = _store()
    backend = _feedback_backend()
    ref = _put(
        store,
        # payload forges a trusted approver name…
        {"case_ref": "c1", "event_type": "kb_promoted", "approved_by": "ceo", "source_id": "rp-1", "event_id": "evt-forge"},
    )
    result = FeedbackNode(store, backend, authorized_approvers=["officer"]).execute(
        # …but the verified caller is not on the allowlist.
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1", "caller_id": "attacker"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["promotion_not_authorized"]
    # KB untouched and no audit row written for the rejected promotion.
    assert backend.records[0]["metadata"].get("kb_promoted") is not True
    assert backend.audit_events == []


def test_kb_promotion_succeeds_for_allowlisted_caller():
    """An allowlisted verified caller promotes the point;
    the audit approver is the caller identity, not the payload's approved_by."""
    store = _store()
    backend = _feedback_backend()
    ref = _put(
        store,
        {"case_ref": "c1", "event_type": "kb_promoted", "approved_by": "victim", "source_id": "rp-1", "event_id": "evt-ok"},
    )
    result = FeedbackNode(store, backend, authorized_approvers=["officer"]).execute(
        {"feedback_payload_ref": ref, "tenant_id": "tenant-a", "session_id": "s", "case_ref": "c1", "caller_id": "officer"}
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["feedback_result"]["kb_updated"] is True
    assert backend.records[0]["metadata"].get("kb_promoted") is True
    # the recorded approver is the verified caller, not the forged payload value.
    assert backend.audit_events[0]["approved_by"] == "officer"


def test_server_minted_case_ref_cannot_be_masked_by_the_s2_gate():
    """case_ref must be lexically incapable of matching detect_pii.

    `_run()` embeds case_ref in the envelope it passes as `user_input`, which is
    one of the SDK's `_PII_SCAN_FIELDS`. The @final S-2 gate MASKS what
    detect_pii matches, so a `str(uuid4())` case_ref was rewritten mid-flight
    for ~0.53% of preflight calls (measured 105/20,000 on this envelope shape).
    A masked case_ref no longer identifies its case, breaking the feedback path
    that looks it up via `case_exists()`.

    A sampled assertion cannot catch a 0.53% event reliably, so this asserts the
    structural property that makes the collision impossible: no digits at all.
    """
    import json

    from shared.security.pii_detector import detect_pii

    from src.payload_store import mint_reference

    for _ in range(500):
        case_ref = mint_reference()
        assert not any(char.isdigit() for char in case_ref), case_ref
        envelope = json.dumps(
            {
                "mode": "preflight",
                "case_ref": case_ref,
                "scope": {"tenant_id": "tenant-a"},
                "document_payload_ref": mint_reference(),
            },
            ensure_ascii=False,
        )
        assert detect_pii(envelope) == [], envelope
