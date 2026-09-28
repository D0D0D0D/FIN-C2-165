"""Stage1 findings reach the officer's candidate list without the LLM's help.

The Stage3 prompt steers the model toward `semantic` findings, so a real LLM
returns no deterministic claim at all. Before this was fixed, a missing required
document and an out-of-tolerance amount appeared under `facts` only — while the
LLM-failure fallback listed them — so a degraded run carried MORE actionable
content than a successful one, and `requires_human_review` stayed false on a
presentation the bank would certainly return.
"""

from __future__ import annotations

from src.nodes.domain_nodes import DRAFT_SECTIONS, Stage4VerifyNode

FACTS_WITH_PROBLEMS = {
    "field_diff": {
        "amount": {"lc": 100_000.0, "doc": 106_000.0, "within_tolerance": False},
        "quantity": {"lc": 1_000.0, "doc": 1_000.0, "within_tolerance": True},
    },
    "docs_present": {"commercial_invoice": True, "insurance_certificate": False},
    "deadline_status": "on_time",
}


def _state(draft: dict, facts: dict | None = None) -> dict:
    facts = facts or FACTS_WITH_PROBLEMS
    return {
        "mode": "preflight",
        "gate_kind": "draft",
        "draft": draft,
        "field_diff": facts["field_diff"],
        "docs_present": facts["docs_present"],
        "deadline_status": facts["deadline_status"],
        "deadline_detail": {},
        "evidence_map": [{"claim_id": "c1", "source_ids": ["rp-1"]}],
        "cited_source_ids": ["rp-1"],
        "retrieved_records": [{"record_id": "rp-1"}],
        "authoritative_sources": [],
        "tenant_id": "t",
        "org_id": "o",
        "session_id": "s",
        "correlation_id": "c",
    }


def _semantic_only_draft() -> dict:
    """What a real LLM returns for this AG: semantic findings, nothing else."""
    return {
        "inconsistency_candidates": [
            {
                "claim_type": "semantic",
                "target": "commercial_invoice",
                "description": "The invoice states Korean origin while the L/C requires Japan.",
                "cited_source_ids": ["rp-1"],
            }
        ],
        "bank_inquiry_points": [],
        "correction_suggestions": [],
    }


def _candidates(result: dict) -> list[dict]:
    return (result.get("preflight_brief") or {}).get("inconsistency_candidates") or []


def test_missing_document_and_tolerance_breach_reach_the_candidate_list():
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft()))
    surfaced = {(c.get("claim_type"), c.get("target")) for c in _candidates(result)}

    assert ("missing_doc", "insurance_certificate") in surfaced, surfaced
    assert ("field_diff", "amount") in surfaced, surfaced
    # the LLM's own finding is kept alongside, not replaced
    assert ("semantic", "commercial_invoice") in surfaced, surfaced


def test_facts_without_a_problem_are_not_surfaced_as_candidates():
    """A present document and an in-tolerance field are not findings."""
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft()))
    surfaced = {(c.get("claim_type"), c.get("target")) for c in _candidates(result)}

    assert ("missing_doc", "commercial_invoice") not in surfaced
    assert ("field_diff", "quantity") not in surfaced
    assert ("deadline", "presentation") not in surfaced, "on_time は所見ではない"


def test_deterministic_descriptions_are_server_authored():
    """The model never authors a factual sentence about a Stage1 fact."""
    lie = "The insurance certificate is present and fully compliant."
    draft = _semantic_only_draft()
    draft["inconsistency_candidates"].append(
        {
            "claim_type": "missing_doc",
            "target": "insurance_certificate",
            "description": lie,
            "cited_source_ids": [],
        }
    )
    result = Stage4VerifyNode().execute(_state(draft))

    descriptions = [c.get("description") for c in _candidates(result)]
    assert lie not in descriptions, "LLM が書いた事実文がそのまま出力された"
    assert any("insurance_certificate" in str(d) for d in descriptions)


def test_deterministic_finding_is_not_duplicated_when_the_llm_also_reports_it():
    draft = _semantic_only_draft()
    draft["inconsistency_candidates"].append(
        {
            "claim_type": "missing_doc",
            "target": "insurance_certificate",
            "description": "whatever the model wrote",
            "cited_source_ids": [],
        }
    )
    result = Stage4VerifyNode().execute(_state(draft))

    hits = [c for c in _candidates(result) if (c["claim_type"], c["target"]) == ("missing_doc", "insurance_certificate")]
    assert len(hits) == 1, f"決定論所見が二重に出た: {hits}"


def test_requires_human_review_flags_stage1_problems():
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft()))
    rhr = result.get("requires_human_review") or {}

    assert rhr.get("flag") is True, rhr
    assert "missing_required_document:insurance_certificate" in rhr.get("reasons", []), rhr
    assert "field_outside_tolerance:amount" in rhr.get("reasons", []), rhr


def test_requires_human_review_stays_false_when_stage1_is_clean():
    clean = {
        "field_diff": {"amount": {"lc": 1.0, "doc": 1.0, "within_tolerance": True}},
        "docs_present": {"commercial_invoice": True},
        "deadline_status": "on_time",
    }
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft(), clean))
    rhr = result.get("requires_human_review") or {}

    assert rhr.get("flag") is False, f"問題が無いのに人間確認が立った: {rhr}"


def test_at_risk_deadline_is_flagged_and_surfaced():
    at_risk = {
        "field_diff": {},
        "docs_present": {"commercial_invoice": True},
        "deadline_status": "at_risk",
    }
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft(), at_risk))

    surfaced = {(c.get("claim_type"), c.get("target")) for c in _candidates(result)}
    assert ("deadline", "presentation") in surfaced, surfaced
    assert "presentation_deadline:at_risk" in (result.get("requires_human_review") or {}).get("reasons", [])


def test_evidence_map_claim_ids_address_the_final_candidate_list():
    """Positional ids minted from the raw draft no longer point at the claim.

    Reconciliation drops contradictions and deterministic findings are
    prepended, so an index-based id built in Stage3 addresses a different claim
    by the time the officer reads the envelope.
    """
    result = Stage4VerifyNode().execute(_state(_semantic_only_draft()))
    brief = result.get("preflight_brief") or {}
    evidence_map = result.get("evidence_map") or []

    expected = [
        (f"{name}:{index}", [sid for sid in (item.get("cited_source_ids") or []) if sid])
        for name in DRAFT_SECTIONS
        for index, item in enumerate(brief.get(name) or [])
    ]
    assert [(e["claim_id"], e["source_ids"]) for e in evidence_map] == expected

    # and the semantic claim's own citation survived the rebuild
    semantic_entry = next(
        e for e, (_, item) in zip(evidence_map, [
            (name, item) for name in DRAFT_SECTIONS for item in (brief.get(name) or [])
        ]) if item.get("claim_type") == "semantic"
    )
    assert semantic_entry["source_ids"] == ["rp-1"]


# ── Stage2 の想起も LLM の引用に依存させない ────────────────────────────────

RECALLED = [
    {"source_id": "br-1", "summary": "Bank returned the set for a missing insurance certificate.",
     "metadata": {"tenant_id": "t"}},
    {"source_id": "br-2", "summary": "Bank queried the invoice amount beyond tolerance.",
     "metadata": {"tenant_id": "t"}},
]


def _state_with_recall(draft: dict) -> dict:
    state = _state(draft)
    state["retrieved_records"] = RECALLED
    state["cited_source_ids"] = ["br-1"]
    state["evidence_map"] = [{"claim_id": "c1", "source_ids": ["br-1"]}]
    return state


def _inquiry(result: dict) -> list[dict]:
    return (result.get("preflight_brief") or {}).get("bank_inquiry_points") or []


def test_recalled_review_points_reach_the_officer_even_if_the_llm_ignores_them():
    """A recall the model never cites is still shown — that is the whole moat."""
    result = Stage4VerifyNode().execute(_state_with_recall(_semantic_only_draft()))
    cited = {sid for item in _inquiry(result) for sid in (item.get("cited_source_ids") or [])}

    assert {"br-1", "br-2"} <= cited, f"想起した過去観点が出力に出ていない: {_inquiry(result)}"


def test_recalled_point_is_not_duplicated_when_the_llm_did_cite_it():
    draft = _semantic_only_draft()
    draft["bank_inquiry_points"] = [
        {
            "claim_type": "semantic",
            "target": "past_bank_return",
            "description": "The model's own wording about br-1.",
            "cited_source_ids": ["br-1"],
        }
    ]
    result = Stage4VerifyNode().execute(_state_with_recall(draft))

    br1 = [i for i in _inquiry(result) if "br-1" in (i.get("cited_source_ids") or [])]
    assert len(br1) == 1, f"LLM が引用済みの観点が二重に出た: {br1}"


def test_recall_text_is_taken_from_the_stored_record_not_from_the_model():
    result = Stage4VerifyNode().execute(_state_with_recall(_semantic_only_draft()))
    appended = [i for i in _inquiry(result) if "br-2" in (i.get("cited_source_ids") or [])]

    assert appended, "br-2 が出ていない"
    assert "invoice amount beyond tolerance" in appended[0]["description"], appended


# ── 検索語の組み立て ────────────────────────────────────────────────────────

from src.nodes.domain_nodes import _recall_query_terms, _safe_kb_text  # noqa: E402


def test_recall_terms_drop_filler_words_produced_by_splitting_identifiers():
    """`at_risk` -> "at" / `certificate_of_origin` -> "of" が残ると崩れる。

    backend は語ごとの部分一致で、順位付けをしない。filler が 1 語混ざると
    ほぼ全ての保存行に当たり、本当に関係する recall が top_k から押し出される。
    """
    terms = _recall_query_terms(
        {"certificate_of_origin": False}, {}, "at_risk"
    ).split()

    assert "of" not in terms and "at" not in terms, terms
    assert "certificate" in terms and "origin" in terms and "risk" in terms, terms


def test_recall_terms_are_empty_when_stage1_found_no_problem():
    assert _recall_query_terms({"commercial_invoice": True}, {"amount": {"within_tolerance": True}}, "on_time") == ""


# ── KB 自由文の関門 ─────────────────────────────────────────────────────────


def test_recall_text_gate_drops_a_sentence_carrying_a_credential():
    assert _safe_kb_text("Note with key sk-aaaaaaaaaaaaaaaaaaaaaaaa inside.") is None


def test_recall_text_gate_masks_contact_details_but_keeps_the_sentence():
    safe = _safe_kb_text("Missing insurance certificate; contact alice@example.com for the copy.")

    assert safe is not None
    assert "alice@example.com" not in safe
    assert "insurance certificate" in safe, "業務文がマスクで壊れた"


def test_recall_text_gate_leaves_ordinary_business_prose_untouched():
    """`name` 判定は Title Case 2 連続に当たる — 業務文を殺さないこと。"""
    prose = "Bank Return: the Commercial Invoice description did not agree with the Packing List."

    assert _safe_kb_text(prose) == prose


def test_cited_source_ids_agree_with_the_rebuilt_evidence_map():
    """`cited_source_ids` は state 内部の値（顧客 envelope には出ない）。

    evidence_map を最終候補から作り直した以上、同じ内容の別表現である
    `cited_source_ids` を Stage3 の値のまま残すと両者が食い違い、
    `claim_source_verified` の cited_count も古い方から数える。
    """
    result = Stage4VerifyNode().execute(_state_with_recall(_semantic_only_draft()))

    from_map = sorted({sid for entry in result["evidence_map"] for sid in entry["source_ids"]})
    assert result["cited_source_ids"] == from_map, (
        f"evidence_map={result['evidence_map']} cited={result['cited_source_ids']}"
    )
    # LLM が引用しなかった recall の id が実際に含まれている（空同士の一致ではない）
    assert "br-2" in from_map, from_map


def test_overlapping_pii_findings_do_not_corrupt_the_sentence():
    """`0900-1234-5678-9012` matches credit_card AND phone_jp."""
    safe = _safe_kb_text("Contact number on file: 0900-1234-5678-9012 for the shipper.")

    assert safe is not None
    assert "masked]asked]" not in safe, f"重なった span の置換で文字列が壊れた: {safe}"
    assert "0900-1234-5678-9012" not in safe
    assert safe.startswith("Contact number on file: ") and safe.endswith(" for the shipper.")


def test_short_trade_document_identifiers_survive_the_query():
    """`bl` / `awb` / `coo` are real documents — a length cutoff erases them."""
    terms = _recall_query_terms({"bl": False, "awb": False, "coo": False}, {}, "on_time").split()

    assert {"bl", "awb", "coo"} <= set(terms), terms
