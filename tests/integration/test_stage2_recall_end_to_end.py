"""Integration — the accumulation loop actually closes over real HTTP.

`/ingest` writes past bank-return review points, `/preflight` must recall them.
This is the end-to-end proof the unit-level retrieval tests cannot give: they
hand the node a hand-written query, while production builds the query from
Stage1 findings. When the two disagree, retrieval returns nothing under
`status: success` and the AG silently degrades to a stateless checker.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.entry_adapter import AuthContext, AuthSource

app = server.app

TENANT = "tenant-recall"

PAST_RETURNS = [
    {
        "observed_at": "2026-03-11",
        "return_id": "br-insurance-001",
        "record_type": "review_point",
        "review_point": (
            "Bank returned the presentation because the insurance certificate "
            "required by the L/C was missing from the document set."
        ),
        "inquiry_reason": "missing insurance certificate",
    },
    {
        "observed_at": "2026-04-02",
        "return_id": "br-amount-002",
        "record_type": "review_point",
        "review_point": (
            "Bank queried the commercial invoice amount because it exceeded the "
            "L/C amount beyond the stated tolerance."
        ),
        "inquiry_reason": "amount beyond tolerance",
    },
    {
        "observed_at": "2026-05-20",
        "return_id": "br-unrelated-003",
        "record_type": "review_point",
        "review_point": "Bank asked about the vessel name spelling on the transport document.",
        "inquiry_reason": "vessel name spelling",
    },
]

PREFLIGHT_PAYLOAD = {
    "lc": {
        "term_id": "lc-term-e2e",
        "required_docs": ["commercial_invoice", "insurance_certificate"],
        "amount": 100_000,
        "tolerance_pct": 5,
        "presentation_period_days": 21,
        "expiry": "2026-09-30",
        "special_conditions": "The commercial invoice must state origin Japan.",
    },
    "documents": {
        "commercial_invoice": {
            "link_id": "doc-invoice-e2e",
            "amount": 106_000,
            "text": "Commercial invoice. Country of origin: Korea.",
        },
        "bill_of_lading": {"link_id": "doc-lading-e2e", "on_board_date": "2026-08-01"},
    },
    "as_of_date": "2026-08-12",
}


def _auth(monkeypatch) -> None:
    def fake_authenticate(_request, *, operation):
        return AuthContext(
            trust=TrustLevel.VERIFIED_EXTERNAL,
            scope={"tenant_id": TENANT},
            principal={"caller_id": "officer-1"},
            auth_source=AuthSource.MIDDLEWARE,
        )

    monkeypatch.setattr(server, "authenticate", fake_authenticate)


def _post(client, route: str, payload) -> dict:
    response = client.post(route, headers={"authorization": "Bearer t"}, json={"payload": payload})
    assert response.status_code == 200, response.text
    return response.json()


def test_ingested_review_points_are_recalled_by_a_later_preflight(monkeypatch):
    _auth(monkeypatch)
    backend = server.services["kb_backend"]
    before = len(backend.records)

    with TestClient(app, raise_server_exceptions=False) as client:
        ingested = _post(client, "/ingest", {"records": PAST_RETURNS})
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    summary = (ingested.get("formatted_output") or {}).get("ingest_summary") or {}
    assert summary.get("written_count") == len(PAST_RETURNS), ingested
    assert len(backend.records) == before + len(PAST_RETURNS)

    envelope = preflight.get("formatted_output") or {}
    assert preflight.get("status") == "success", preflight
    assert envelope.get("degradation_reason") != "no scoped retrieval hits", (
        "Stage2 recalled nothing although matching review points were ingested "
        "through the production path — the accumulation loop is write-only."
    )

    recalled = " ".join(
        str(point.get("description", ""))
        for point in (envelope.get("bank_inquiry_points") or [])
    )
    assert "insurance certificate" in recalled, (
        f"the ingested insurance-certificate review point was not recalled: {recalled!r}"
    )


def test_recall_is_driven_by_stage1_findings_not_by_every_stored_row(monkeypatch):
    """A stored row unrelated to the Stage1 findings must not be recalled.

    Without this, a query that matches everything would also make the assertion
    above pass while telling the officer nothing.
    """
    _auth(monkeypatch)

    with TestClient(app, raise_server_exceptions=False) as client:
        _post(client, "/ingest", {"records": PAST_RETURNS})
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    envelope = preflight.get("formatted_output") or {}
    recalled = " ".join(
        str(point.get("description", ""))
        for point in (envelope.get("bank_inquiry_points") or [])
    )
    assert "vessel name" not in recalled, (
        f"an unrelated stored row was recalled — the query matches indiscriminately: {recalled!r}"
    )


# ── 正常な LLM 経路（縮退でない）で補完が働くか ─────────────────────────────
#
# 上の 2 本は runtime の LLM が未設定なので `_stage3_fallback` が走り、recall は
# fallback 側が載せている。実 LLM が「成功したうえで recall を引用しない」経路
# （実測でそうなった）は、それでは一度も通らない。


class _SemanticOnlyLLM:
    """What the real provider actually returned for this AG.

    Semantic findings only, `bank_inquiry_points` empty — no deterministic claim
    and no citation of the recalled review points. Framework-supplied keys are
    deliberately absent so their omission stays detectable.
    """

    def complete(self, messages: list) -> dict:
        import json as _json

        return {
            "content": _json.dumps(
                {
                    "inconsistency_candidates": [
                        {
                            "claim_type": "semantic",
                            "target": "commercial_invoice",
                            "description": "The invoice states Korean origin while the L/C requires Japan.",
                            "cited_source_ids": ["doc-invoice-e2e"],
                        }
                    ],
                    "bank_inquiry_points": [],
                    "correction_suggestions": [],
                }
            ),
            "model": "fake",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": "fake"},
        }

    def stream(self, messages: list):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list):
        return self


def test_recall_reaches_the_envelope_when_the_llm_succeeds_but_cites_nothing(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setitem(server.services, "llm", _SemanticOnlyLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        _post(client, "/ingest", {"records": PAST_RETURNS})
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    envelope = preflight.get("formatted_output") or {}
    assert preflight.get("status") == "success", preflight
    # the LLM's own finding survived — this is not the fallback path
    assert any(
        "Korean origin" in str(c.get("description"))
        for c in (envelope.get("inconsistency_candidates") or [])
    ), envelope

    recalled = " ".join(
        str(p.get("description", "")) for p in (envelope.get("bank_inquiry_points") or [])
    )
    assert "insurance certificate" in recalled, (
        f"LLM が引用しなかった recall が担当者に届いていない: {recalled!r}"
    )


# ── KB に入った自由文が顧客向け envelope へ抜けないこと ─────────────────────

# The row must NOT contain any word from `_PROHIBITED_ROW_TOKENS`
# ("password" / "api_key" / "secret" / "token" / "credential"): the older
# name-based check would then reject it and the value-based gate under test
# would never be exercised (a mutation removing it stayed green until this
# input was fixed).
CREDENTIAL_ROW = {
    "observed_at": "2026-06-01",
    "return_id": "br-cred-900",
    "record_type": "review_point",
    "review_point": "Bank rejected the set; ops pasted sk-aaaaaaaaaaaaaaaaaaaaaaaa into this note.",
    "inquiry_reason": "value pasted into the note",
}

CONTACT_ROW = {
    "observed_at": "2026-06-02",
    "return_id": "br-pii-901",
    "record_type": "review_point",
    "review_point": (
        "Bank returned the presentation for a missing insurance certificate; "
        "contact alice@example.com to obtain the replacement."
    ),
    "inquiry_reason": "missing insurance certificate",
}


def test_ingest_rejects_a_row_carrying_a_credential(monkeypatch):
    _auth(monkeypatch)
    backend = server.services["kb_backend"]

    with TestClient(app, raise_server_exceptions=False) as client:
        result = _post(client, "/ingest", {"records": [CREDENTIAL_ROW]})

    summary = (result.get("formatted_output") or {}).get("ingest_summary") or {}
    assert summary.get("written_count") == 0, summary
    assert summary.get("rejected_count") == 1, summary
    stored = " ".join(str(r.get("summary", "")) for r in backend.records)
    assert "sk-aaaa" not in stored, "credential が KB に書かれた"
    # the rejection reason must not echo the secret back
    assert "sk-aaaa" not in json_dumps(summary), summary


def test_a_credential_already_in_the_kb_never_reaches_the_envelope(monkeypatch):
    """Rows stored before the ingest gate existed still must not surface."""
    _auth(monkeypatch)
    backend = server.services["kb_backend"]
    backend.upsert(
        {
            "source_id": "br-legacy-902",
            "title": "review_point",
            "summary": "Legacy note about the missing insurance certificate: key sk-bbbbbbbbbbbbbbbbbbbbbbbb.",
            "snippet": "",
            "metadata": {"tenant_id": TENANT, "record_type": "review_point", "kb_promoted": True},
        }
    )
    monkeypatch.setitem(server.services, "llm", _SemanticOnlyLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    body = json_dumps(preflight)
    assert "sk-bbbb" not in body, "KB 由来の credential が顧客向け envelope に出た"


def test_contact_details_in_a_recalled_note_are_masked(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setitem(server.services, "llm", _SemanticOnlyLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        _post(client, "/ingest", {"records": [CONTACT_ROW]})
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    body = json_dumps(preflight)
    assert "alice@example.com" not in body, "recall 経由で連絡先がそのまま出た"
    # the business sentence itself must survive the masking
    assert "insurance certificate" in body


def json_dumps(value) -> str:
    import json as _json

    return _json.dumps(value, ensure_ascii=False)


class _SecretCopyingLLM(_SemanticOnlyLLM):
    """A model that copies a stored secret into its own text and cites the source.

    Gating only the recalls the model failed to cite left this open: the citation
    put the source id into `already`, the safety check was skipped, and the
    secret rode out inside a `semantic` claim the model authored.
    """

    def complete(self, messages: list) -> dict:
        import json as _json

        return {
            "content": _json.dumps(
                {
                    "inconsistency_candidates": [],
                    "bank_inquiry_points": [
                        {
                            "claim_type": "semantic",
                            "target": "past_bank_return",
                            "description": "Earlier note says the gateway string sk-bbbbbbbbbbbbbbbbbbbbbbbb applies here.",
                            "cited_source_ids": ["br-legacy-902"],
                        }
                    ],
                    "correction_suggestions": [],
                }
            ),
            "model": "fake",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": "fake"},
        }


def _seed_legacy_secret(backend) -> None:
    backend.upsert(
        {
            "source_id": "br-legacy-902",
            "title": "review_point",
            "summary": "Legacy note about the missing insurance certificate: sk-bbbbbbbbbbbbbbbbbbbbbbbb.",
            "snippet": "",
            "metadata": {"tenant_id": TENANT, "record_type": "review_point", "kb_promoted": True},
        }
    )


def test_a_model_that_copies_a_stored_secret_and_cites_it_cannot_ship_it(monkeypatch):
    _auth(monkeypatch)
    _seed_legacy_secret(server.services["kb_backend"])
    monkeypatch.setitem(server.services, "llm", _SecretCopyingLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    body = json_dumps(preflight)
    assert "sk-bbbb" not in body, "LLM が複写した secret が envelope に出た"


def test_suppressing_a_recall_is_visible_to_the_officer(monkeypatch):
    """A past review point must not vanish silently."""
    _auth(monkeypatch)
    _seed_legacy_secret(server.services["kb_backend"])
    monkeypatch.setitem(server.services, "llm", _SemanticOnlyLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    rhr = (preflight.get("formatted_output") or {}).get("requires_human_review") or {}
    assert rhr.get("flag") is True, rhr
    assert any("suppressed" in str(r) for r in rhr.get("reasons", [])), rhr
    # the reason must not carry the value it suppressed
    assert "sk-bbbb" not in json_dumps(rhr)


def test_a_bearer_looking_business_sentence_is_still_ingestable(monkeypatch):
    """`Bearer BILL...` is trade wording, not a credential handoff."""
    _auth(monkeypatch)
    row = {
        "observed_at": "2026-06-03",
        "return_id": "br-bearer-903",
        "record_type": "review_point",
        "review_point": "Bearer BILL1234567890123456 must be endorsed before presentation.",
        "inquiry_reason": "endorsement missing",
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        result = _post(client, "/ingest", {"records": [row]})

    summary = (result.get("formatted_output") or {}).get("ingest_summary") or {}
    assert summary.get("written_count") == 1, f"正当な業務行が reject された: {summary}"


def test_a_credential_split_across_fields_is_not_manufactured(monkeypatch):
    """Concatenating fields must not synthesize a signature no field carries."""
    _auth(monkeypatch)
    row = {
        "observed_at": "2026-06-04",
        "return_id": "br-split-904",
        "record_type": "review_point",
        "review_point": "Bearer",
        "inquiry_reason": "ABCDEFGHIJKLMNOP",
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        result = _post(client, "/ingest", {"records": [row]})

    summary = (result.get("formatted_output") or {}).get("ingest_summary") or {}
    assert summary.get("written_count") == 1, f"フィールド境界をまたぐ偽陽性で reject された: {summary}"


class _SecretInventingLLM(_SemanticOnlyLLM):
    """The model writes a credential of its own and cites a LEGITIMATE source.

    The KB-boundary sanitizer cannot help here — nothing was recalled. Only the
    Stage4 candidate check stands between this and the officer's envelope, so
    this is the case that proves that layer carries weight (a mutation removing
    it stayed green until this test existed).
    """

    def complete(self, messages: list) -> dict:
        import json as _json

        return {
            "content": _json.dumps(
                {
                    "inconsistency_candidates": [
                        {
                            "claim_type": "semantic",
                            "target": "commercial_invoice",
                            "description": "The invoice header embeds sk-cccccccccccccccccccccccc as a reference.",
                            "cited_source_ids": ["doc-invoice-e2e"],
                        }
                    ],
                    "bank_inquiry_points": [],
                    "correction_suggestions": [],
                }
            ),
            "model": "fake",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": "fake"},
        }


def test_a_model_that_invents_a_credential_cannot_ship_it(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setitem(server.services, "llm", _SecretInventingLLM())

    with TestClient(app, raise_server_exceptions=False) as client:
        preflight = _post(client, "/preflight", PREFLIGHT_PAYLOAD)

    body = json_dumps(preflight)
    # ① 最重要の主張は不変 — LLM が書いた credential が呼び出し元へ出ないこと。
    assert "sk-cccc" not in body, "LLM が書いた credential が envelope に出た"

    # ② 遮断が「見える形」で起きていること。**拒否地点は SDK の版で変わる**ので両方を許す:
    #    - 1.0.0: AG 自身が抑止し formatted_output.requires_human_review.reasons に "suppressed"
    #    - 1.0.1: framework の S-3 出力ゲートが Stage3ApplyNode でブロックし、
    #             subgraph 失敗として error_log に "S-3 output gate" が入る（より早く fail-closed）
    #    どちらも遮断は維持されている。片方だけを assert すると版上げで**誤って赤くなる**
    #    （2026-08-19 に 1.0.1 化で実測）。
    rhr = (preflight.get("formatted_output") or {}).get("requires_human_review") or {}
    suppressed = any("suppressed" in str(r) for r in rhr.get("reasons", []))
    gate_blocked = any(
        "S-3 output gate" in str(e) and "credential" in str(e)
        for e in (preflight.get("error_log") or [])
    )
    assert suppressed or gate_blocked, {
        "requires_human_review": rhr,
        "error_log": preflight.get("error_log"),
    }


def test_the_kb_never_accumulates_raw_contact_details(monkeypatch):
    """Mask at WRITE time, not only on read.

    Masking on read leaves the raw value in the store: a DB dump, a checkpoint,
    or one overlooked read path then exposes every row ever ingested. The
    established pattern (Professional Services Engagement Acceptance Evidence Agent / `accumulation-ledger`) derives the
    model-visible value once, at write.
    """
    _auth(monkeypatch)
    backend = server.services["kb_backend"]

    with TestClient(app, raise_server_exceptions=False) as client:
        result = _post(client, "/ingest", {"records": [CONTACT_ROW]})

    assert ((result.get("formatted_output") or {}).get("ingest_summary") or {}).get("written_count") == 1
    stored = " ".join(
        f"{r.get('summary', '')} {r.get('snippet', '')}"
        for r in backend.records
        if (r.get("metadata") or {}).get("tenant_id") == TENANT
    )
    assert "alice@example.com" not in stored, f"KB に生の連絡先が蓄積された: {stored!r}"
    assert "[email masked]" in stored, stored
    # the business sentence is still searchable after masking
    assert "insurance certificate" in stored
