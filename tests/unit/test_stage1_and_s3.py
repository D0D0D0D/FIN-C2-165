"""Unit tests — Stage1 deterministic facts and the S-3 assertion-lexicon gate.

These cover the two domain cores that a passing test run alone would not verify:
the deterministic LC-collation facts (design §9-1) and the S-3 output gate
(design §2-6 / §9-2). Both PASS-expected and BLOCK/REDACT-expected inputs are
exercised.
"""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from src.nodes.domain_nodes import Stage1DeterministicNode
from src.nodes.post_process_node import PostProcessNode, _has_assertion
from src.payload_store.payload_store import PayloadStore
from src.scope import SCOPE_KEYS, scope_for


def _stage1(payload, tenant="tenant-a", session="sess-a"):
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = store.put(payload, scope=scope_for(tenant), session_id=session)
    node = Stage1DeterministicNode(store)
    return node.execute(
        {"plan_draft_ref": ref, "tenant_id": tenant, "session_id": session}
    )


# ── Stage 1: numeric tolerance ───────────────────────────────────────────────


def test_amount_within_tolerance_is_true():
    result = _stage1(
        {"lc": {"required_docs": ["invoice"], "amount": 10000, "tolerance_pct": 5}, "documents": {"invoice": {"amount": 10400}}}
    )
    assert result["field_diff"]["amount"]["within_tolerance"] is True


def test_amount_beyond_tolerance_is_false():
    result = _stage1(
        {"lc": {"required_docs": ["invoice"], "amount": 10000, "tolerance_pct": 5}, "documents": {"invoice": {"amount": 10600}}}
    )
    assert result["field_diff"]["amount"]["within_tolerance"] is False


def test_missing_required_document_flagged():
    result = _stage1(
        {"lc": {"required_docs": ["invoice", "packing_list"]}, "documents": {"invoice": {"amount": 1}}}
    )
    assert result["docs_present"] == {"invoice": True, "packing_list": False}


def test_field_diff_uses_invoice_not_first_document():
    # The commercial invoice is authoritative — a packing_list amount
    # must not win by dict ordering.
    result = _stage1(
        {
            "lc": {"amount": 100, "tolerance_pct": 0},
            "documents": {"packing_list": {"amount": 10}, "invoice": {"amount": 100}},
        }
    )
    assert result["field_diff"]["amount"]["doc"] == 100.0
    assert result["field_diff"]["amount"]["within_tolerance"] is True


def test_field_diff_without_invoice_value_flags_review():
    result = _stage1(
        {"lc": {"amount": 100, "tolerance_pct": 5}, "documents": {"packing_list": {"amount": 100}}}
    )
    assert result["field_diff"]["amount"]["within_tolerance"] is False
    assert result["field_diff"]["amount"]["reason"] == "invoice_value_absent"


# ── Stage 1: presentation deadline (design §9-1) ─────────────────────────────


def test_deadline_on_time_uses_min_of_expiry_and_period():
    # on_board 2026-02-01 + 21 = 2026-02-22; expiry 2026-03-01 -> min = 02-22.
    result = _stage1(
        {
            "lc": {"presentation_period_days": 21, "expiry": "2026-03-01"},
            "documents": {"bill_of_lading": {"on_board_date": "2026-02-01"}},
            "as_of_date": "2026-02-10",
        }
    )
    assert result["deadline_status"] == "on_time"
    assert result["deadline_detail"]["days_remaining"] == 12
    assert result["deadline_detail"]["date_basis"] == "on_board_date"


def test_deadline_late_when_past_effective_deadline():
    result = _stage1(
        {
            "lc": {"presentation_period_days": 21, "expiry": "2026-03-01"},
            "documents": {"bill_of_lading": {"on_board_date": "2026-02-01"}},
            "as_of_date": "2026-02-25",
        }
    )
    assert result["deadline_status"] == "late"
    assert result["deadline_detail"]["days_remaining"] < 0


def test_deadline_at_risk_within_threshold():
    result = _stage1(
        {
            "lc": {"presentation_period_days": 21, "expiry": "2026-03-01"},
            "documents": {"bill_of_lading": {"on_board_date": "2026-02-01"}},
            "as_of_date": "2026-02-20",
        }
    )
    assert result["deadline_status"] == "at_risk"


def test_deadline_falls_back_to_bl_issue_date_and_notes_basis():
    result = _stage1(
        {
            "lc": {"presentation_period_days": 21},
            "documents": {"bill_of_lading": {"issue_date": "2026-02-01"}},
            "as_of_date": "2026-02-10",
        }
    )
    assert result["deadline_detail"]["date_basis"] == "bl_issue_date"


def test_deadline_defaults_to_ucp600_21_days_when_period_absent():
    result = _stage1(
        {"lc": {}, "documents": {"bill_of_lading": {"on_board_date": "2026-02-01"}}, "as_of_date": "2026-02-10"}
    )
    assert result["deadline_detail"]["applicable_period"] == 21


def test_stage1_scope_missing_fails_closed():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = store.put({"lc": {}, "documents": {}}, scope=scope_for("tenant-a"), session_id="s")
    result = Stage1DeterministicNode(store).execute(
        {"plan_draft_ref": ref, "tenant_id": "", "session_id": "s"}
    )
    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["scope_missing"]


# ── S-3 assertion lexicon: detection both directions ─────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "The bank must accept the documents.",
        "銀行は書類を受理すべきである。",
        "Documents are genuine and authentic.",
        "書類は真正である。",
        "discrepancies are waived",
        "payment is guaranteed",
        "ｔｈｅ ｂａｎｋ ｍｕｓｔ ａｃｃｅｐｔ",  # full-width bypass attempt (NFKC)
    ],
)
def test_assertion_lexicon_detects_verdicts(text):
    assert _has_assertion(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Please confirm the invoice amount with the bank.",
        "L/C amount and document amount differ beyond tolerance.",
        "銀行に照会が必要な論点です。",
        "Required document 'packing_list' appears to be missing.",
    ],
)
def test_assertion_lexicon_allows_advisory_text(text):
    assert _has_assertion(text) is False


# ── S-3 gate: redaction + demotion + structural verdict block ────────────────


def _preflight_envelope(**overrides):
    base = {
        "mode": "preflight",
        "gate_kind": "draft",
        "facts": {},
        "evidence_map": [],
        "inconsistency_candidates": [],
        "bank_inquiry_points": [],
        "correction_suggestions": [],
        "requires_human_review": {"flag": False, "reasons": []},
    }
    base.update(overrides)
    return base


def test_s3_redacts_assertion_and_demotes_to_human_review():
    node = PostProcessNode()
    out = node._sanitize_client_echoes(
        _preflight_envelope(
            inconsistency_candidates=[{"description": "The bank must accept the documents."}],
            correction_suggestions=[{"text": "書類は真正である。"}],
        )
    )
    assert "redacted" in out["inconsistency_candidates"][0]["description"]
    assert "redacted" in out["correction_suggestions"][0]["text"]
    assert out["requires_human_review"]["flag"] is True
    assert out["requires_human_review"]["reasons"]


def test_s3_leaves_advisory_text_and_review_flag_untouched():
    node = PostProcessNode()
    out = node._sanitize_client_echoes(
        _preflight_envelope(bank_inquiry_points=[{"description": "Please confirm amount with the bank."}])
    )
    assert out["bank_inquiry_points"][0]["description"] == "Please confirm amount with the bank."
    assert out["requires_human_review"]["flag"] is False


@pytest.mark.parametrize("bad_key", ["payment_verdict", "presentation_approved", "authenticity_verdict", "discrepancy_final"])
def test_s3_blocks_structural_verdict_fields(bad_key):
    node = PostProcessNode()
    with pytest.raises(SecurityViolationError, match="prohibited verdict field"):
        node._sanitize_client_echoes(_preflight_envelope(**{bad_key: "X"}))


def test_s3_blocks_nested_verdict_field():
    node = PostProcessNode()
    with pytest.raises(SecurityViolationError, match="prohibited verdict field"):
        node._sanitize_client_echoes(
            _preflight_envelope(inconsistency_candidates=[{"description": "ok", "payment_verdict": "PAY"}])
        )


# Verdict keys in camelCase / full-width / suffix variants
# must all be blocked (normalized key match, not exact casefold).
@pytest.mark.parametrize(
    "bad_key",
    [
        "paymentVerdict",
        "PaymentVerdict",
        "payment-verdict",
        "ＰＡＹＭＥＮＴ＿ＶＥＲＤＩＣＴ",
        "shipmentAuthenticityCheck",  # matches 'authentic' keyword
        "finalPresentationApproved",  # matches 'approved' suffix
        "isAccepted",
        "paymentAccepted",
        "acceptanceStatus",
        # Japanese verdict keys must not slip through the ASCII strip.
        "支払可否",
        "提示承認",
        "真正性判定",
        "受理ステータス",
    ],
)
def test_s3_blocks_verdict_key_variants(bad_key):
    node = PostProcessNode()
    with pytest.raises(SecurityViolationError, match="prohibited verdict field"):
        node._sanitize_client_echoes(_preflight_envelope(**{bad_key: "X"}))


# An assertion written into ANY free-text key (not just the
# enumerated ones) must be redacted — recursive scan, not a path list.
@pytest.mark.parametrize("free_key", ["rationale", "note", "detail", "explanation", "summary_text"])
def test_s3_redacts_assertion_in_arbitrary_free_text_key(free_key):
    node = PostProcessNode()
    out = node._sanitize_client_echoes(
        _preflight_envelope(bank_inquiry_points=[{free_key: "The bank must accept the documents."}])
    )
    assert "redacted" in out["bank_inquiry_points"][0][free_key]
    assert out["requires_human_review"]["flag"] is True


def test_s3_scans_nested_requires_human_review_smuggle_key():
    # a nested key literally named requires_human_review must NOT be skipped
    # (only the ROOT control key is preserved).
    node = PostProcessNode()
    out = node._sanitize_client_echoes(
        _preflight_envelope(
            bank_inquiry_points=[{"requires_human_review": {"reason": "The bank must accept the documents."}}]
        )
    )
    assert "redacted" in out["bank_inquiry_points"][0]["requires_human_review"]["reason"]
    assert out["requires_human_review"]["flag"] is True


def test_s3_deeply_nested_assertion_does_not_escape_via_truncation():
    # deep nesting must be scanned before the base depth-8 truncation, so a
    # buried assertion is flagged and its raw text never surfaces.
    node = PostProcessNode()
    nested: dict = {"leaf": "The bank must accept the documents."}
    for i in range(15):
        nested = {f"k{i}": nested}
    out = node._sanitize_client_echoes(_preflight_envelope(inconsistency_candidates=[nested]))
    import json

    assert "must accept" not in json.dumps(out)
    assert out["requires_human_review"]["flag"] is True
