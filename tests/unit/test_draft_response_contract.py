"""Stage3 のプロンプトが「Stage4 が実際に読む形」を宣言していることを固定する。

なぜ要るか（2026-08-12 の棚卸し）:
  Stage4 は draft から ``inconsistency_candidates`` / ``bank_inquiry_points`` /
  ``correction_suggestions`` の 3 セクションを読むが、Stage3 のプロンプトは候補 1 件の形しか
  伝えておらず **セクション名を一度も言っていなかった**。``draft.get(name, [])`` は未知キーに
  対して空リストを返すので、モデルが別の名前で束ねた瞬間に**全候補が黙って消え、
  `status: success` のまま空のブリーフが返る**。この AG の中核（L/C 書類の照合候補）が
  丸ごと失われるのに、例外もログも degradation も立たない。

  同型の実装（SDSRevisionEvidenceAgent）は不一致が例外になるぶん気づけたが、こちらは沈黙する。

検査の立て方:
  宣言と定数の一致を assert しても意味がない（同じ定数から作っているので常に真）。
  **宣言だけを見て応答を組み立て、Stage4 の読み出しに通す**。宣言が実態からズレた瞬間に赤くなる。
"""
from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode
from src.nodes.domain_nodes import (
    Stage4VerifyNode,
    CANDIDATE_KEYS,
    CLAIM_TYPES,
    DRAFT_SECTIONS,
    _draft_response_contract,
    _reconcile_candidates,
    _stage3_evidence,
    _stage3_prompt,
)

FACTS = {
    "field_diff": {"amount": {"within_tolerance": False, "detail": "1,000 vs 1,200"}},
    "docs_present": {"invoice": False},
    "deadline_status": "expired",
    "deadline_detail": {},
}


def _candidate_from_declaration(claim_type: str, target: str) -> dict:
    """contract の candidate_schema だけを見て 1 件組み立てる（実装を覗かない）."""
    schema = _draft_response_contract()["candidate_schema"]
    item = {}
    for key in schema:
        if key == "claim_type":
            item[key] = claim_type
        elif key == "target":
            item[key] = target
        elif key == "cited_source_ids":
            item[key] = ["rp-1"]
        else:
            item[key] = "宣言どおりに書いた本文"
    return item


def test_prompt_embeds_the_contract():
    """契約関数を作っただけでプロンプトへ配線し忘れる変異を捕まえる。"""
    prompt = json.loads(_stage3_prompt({"lc_special_conditions": "", "document_texts": {}}))
    assert prompt["response_contract"] == _draft_response_contract()
    assert prompt["response_contract"]["top_level_keys"], "セクション名を伝えていない"


def test_a_draft_built_only_from_the_declared_section_names_survives():
    """宣言どおりのセクション名なら候補が残ること。

    ここが本体 — 宣言と読み出しがズレていれば、この assert が空リストで落ちる。
    """
    draft = {
        name: [_candidate_from_declaration("field_diff", "amount")]
        for name in _draft_response_contract()["top_level_keys"]
    }
    for name in DRAFT_SECTIONS:
        kept = _reconcile_candidates(draft.get(name, []), FACTS, [], require_source=True)
        assert kept, f"{name}: 宣言どおりに答えた候補が残らなかった"


def test_a_section_named_outside_the_declaration_is_dropped_in_silence():
    """宣言外の名前は黙って消える — だからこそ宣言が要る、という性質の固定。"""
    draft = {"findings": [_candidate_from_declaration("field_diff", "amount")]}
    for name in DRAFT_SECTIONS:
        assert _reconcile_candidates(draft.get(name, []), FACTS, [], require_source=True) == []


@pytest.mark.parametrize("claim_type", sorted(CLAIM_TYPES))
def test_every_declared_claim_type_is_understood_by_reconcile(claim_type):
    """宣言した claim_type が 1 つでも黙殺されるなら、宣言が実態より広い。"""
    target = {"field_diff": "amount", "missing_doc": "invoice",
              "deadline": "presentation", "semantic": "special_condition"}[claim_type]
    kept = _reconcile_candidates(
        [_candidate_from_declaration(claim_type, target)], FACTS, [], require_source=True
    )
    assert kept, f"claim_type={claim_type} が落とされた"
    assert set(kept[0]) == set(CANDIDATE_KEYS), "顧客出力候補が閉じたキー集合でない"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("description", {"raw_document_text": "RAW-DOC-SENTINEL"}),
        ("cited_source_ids", [{"raw": "RAW-CITATION-SENTINEL"}]),
        ("target", {"raw": "RAW-TARGET-SENTINEL"}),
        ("claim_type", "undeclared_claim_type"),
    ],
    ids=("nested-dict", "dict-in-list", "non-string", "undeclared-enum"),
)
def test_reconcile_rejects_typed_candidate_contract_violations(field, invalid_value):
    candidate = _candidate_from_declaration("semantic", "special_condition")
    candidate[field] = invalid_value

    with pytest.raises(SecurityViolationError, match="S-3 finding projection"):
        _reconcile_candidates([candidate], FACTS, [], require_source=True)


def test_deterministic_claim_types_are_declared_as_such():
    contract = _draft_response_contract()
    assert set(contract["deterministic_claim_types"]) < set(contract["candidate_schema"]["claim_type"])
    assert "semantic" not in contract["deterministic_claim_types"]


# --- 業務不変条件（実装をどう変えても動かせない性質） ------------------------
# セクション名は docs/02_design.md §出力契約が「計 6 フィールド」として顧客に約束している
# 名前であり、設計値ではない。定数化しただけでは宣言も読み出しも一緒に動いてしまうので
# （セクション名を 1 つ変える変異が全緑だった）、名前そのものと、独立にハードコードしている
# post_process の射影との一致を固定する。

CONTRACTED_SECTIONS = ("inconsistency_candidates", "bank_inquiry_points", "correction_suggestions")


def test_section_names_are_the_ones_promised_in_the_output_contract():
    """docs/02_design.md の 6 フィールド envelope が約束している名前であること。"""
    assert DRAFT_SECTIONS == CONTRACTED_SECTIONS


def test_post_process_projects_exactly_the_sections_the_assembler_writes():
    """assembler が書く名前と post_process が読む名前が一致すること。

    両者は独立にキー名を持つ（`domain_nodes.DRAFT_SECTIONS` と
    `post_process_node.format_preflight` のリテラル）。片方だけ変えると
    envelope が空になるのに、どちらのモジュールのテストも緑のまま通る。
    """
    brief = {"facts": {}}
    for name in DRAFT_SECTIONS:
        brief[name] = [_candidate_from_declaration("semantic", "special_condition")]
    formatted = PostProcessNode().format_preflight(
        {"preflight_brief": brief, "requires_human_review": {"flag": True, "reasons": []}}
    )
    for name in DRAFT_SECTIONS:
        assert formatted.get(name), f"{name}: assembler が書いた候補が envelope に出ていない"


def test_declared_sections_survive_all_the_way_into_the_envelope():
    """宣言 → assembler → envelope が 1 本につながっていること（宣言だけを見て組む）。"""
    declared = _draft_response_contract()["top_level_keys"]
    draft = {name: [_candidate_from_declaration("field_diff", "amount")] for name in declared}
    brief = {"facts": FACTS}
    for name in DRAFT_SECTIONS:
        brief[name] = _reconcile_candidates(draft.get(name, []), FACTS, [], require_source=True)
    formatted = PostProcessNode().format_preflight(
        {"preflight_brief": brief, "requires_human_review": {"flag": True, "reasons": []}}
    )
    assert all(formatted.get(name) for name in CONTRACTED_SECTIONS), (
        "宣言どおりに答えた候補が envelope まで届いていない"
    )


def _stage4_state(draft: dict) -> dict:
    return {
        "mode": "preflight", "gate_kind": "draft", "draft": draft,
        "field_diff": {"amount": {"within_tolerance": False, "detail": "1,000 vs 1,200"}},
        "docs_present": {}, "deadline_status": "on_time", "deadline_detail": {},
        "evidence_map": [{"claim_id": "c1", "source_ids": ["rp-1"]}], "cited_source_ids": ["rp-1"],
        "retrieved_records": [{"record_id": "rp-1"}], "authoritative_sources": [],
        "tenant_id": "t", "org_id": "o", "session_id": "s", "correlation_id": "c",
    }


def test_stage4_assembles_exactly_the_declared_sections():
    """assembler が宣言セクションを読むこと。

    `_reconcile_candidates` を直接呼ぶだけでは、assembler が別の名前を読む変異
    （`for name in ("findings",)`）を取り逃す — 実際に素通りした。ノードを通す。
    """
    declared = _draft_response_contract()["top_level_keys"]
    draft = {name: [_candidate_from_declaration("field_diff", "amount")] for name in declared}
    result = Stage4VerifyNode().execute(_stage4_state(draft))
    brief = result.get("preflight_brief") or {}
    assert set(brief) == {"facts", *CONTRACTED_SECTIONS}, f"brief のキーが契約と違う: {sorted(brief)}"
    for name in CONTRACTED_SECTIONS:
        assert brief.get(name), f"{name}: 宣言どおりに答えた候補が assembler で消えた"


def _semantic_descriptions(brief: dict) -> list[str]:
    return [
        item.get("description")
        for name in CONTRACTED_SECTIONS
        for item in (brief.get(name) or [])
        if item.get("claim_type") == "semantic"
    ]


def test_stage4_keeps_a_semantic_claim_placed_in_a_declared_section():
    """宣言内に置いた LLM 由来の所見は残る（下のテストの対照実験）。

    これが無いと「宣言外だから消えた」のか「semantic はそもそも通らない」のかを
    区別できず、下のテストが vacuous になる。
    """
    result = Stage4VerifyNode().execute(
        _stage4_state({"inconsistency_candidates": [_candidate_from_declaration("semantic", "invoice")]})
    )
    assert _semantic_descriptions(result.get("preflight_brief") or {}), (
        "宣言内に置いた semantic 所見が assembler で消えた"
    )


def test_stage4_drops_a_section_the_declaration_does_not_name():
    """宣言外の名前で束ねると LLM の所見は消える（この AG が沈黙する理由の固定）。

    決定論所見（欠落書類・許容差超過）は assembler が Stage1 facts から補完するため
    候補リスト自体は空にならない（`_ensure_deterministic_candidates`）。ここで固定
    したいのは「宣言外セクションに入れた LLM の答えが採用されない」ことなので、
    LLM しか書けない semantic 所見の有無で見る。
    """
    result = Stage4VerifyNode().execute(
        _stage4_state({"findings": [_candidate_from_declaration("semantic", "invoice")]})
    )
    assert not _semantic_descriptions(result.get("preflight_brief") or {}), (
        "宣言外セクションに置いた LLM 所見が採用された"
    )


def test_deterministic_fallback_fills_every_declared_section():
    """LLM 不在の縮退でも宣言セクションが全部揃うこと。"""
    from src.nodes.domain_nodes import _stage3_fallback

    fallback = _stage3_fallback({"field_diff": {}, "docs_present": {}, "recalled_review_points": []})
    assert set(fallback) == set(DRAFT_SECTIONS)


def test_evidence_collection_covers_every_declared_section():
    """引用証跡の収集も宣言セクションを見ること。

    ここが別名を読むと `evidence_map` が空になり、S-3 provenance の裏付けが
    静かに消える（候補は残るのに出典が無い状態）。
    """
    from src.nodes.domain_nodes import _stage3_evidence

    declared = _draft_response_contract()["top_level_keys"]
    for name in declared:
        draft = {name: [_candidate_from_declaration("semantic", "special_condition")]}
        _points, cited = _stage3_evidence({"retrieved_records": [{"record_id": "rp-1"}]}, draft)
        assert cited, f"{name}: 宣言セクションの候補から引用が拾われていない"


def test_stage4_rejects_undeclared_candidate_keys_before_customer_output():
    """LLM-authored candidate dictionaries are closed, not passthrough maps.

    A raw document can otherwise ride an undeclared key through Stage4 and the
    output envelope unchanged.  Exercise the callable node so S-1/S-2/execute/
    S-3 run in the same order as production.
    """
    bad = {
        "claim_type": "semantic",
        "target": "t",
        "description": "d",
        "cited_source_ids": ["rp-1"],
        "raw_document_text": "RAW-SENTINEL",
    }
    draft = {name: [dict(bad)] for name in DRAFT_SECTIONS}
    source_state = {
        "retrieved_records": [
            {
                "source_id": "rp-1",
                "title": "known",
                "summary": "known",
                "snippet": "known",
                "metadata": {"tenant_id": "tenant-a"},
            }
        ],
        "authoritative_sources": [],
    }
    evidence_map, cited = _stage3_evidence(source_state, draft)
    state = {
        **source_state,
        "tenant_id": "tenant-a",
        "mode": "preflight",
        "status": "success",
        "gate_kind": "draft",
        "draft": draft,
        "evidence_map": evidence_map,
        "cited_source_ids": cited,
        "docs_present": {},
        "field_diff": {},
        "deadline_status": "unknown",
        "deadline_detail": {},
        "requires_human_review": {"flag": False, "reasons": []},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
    }

    verified = Stage4VerifyNode()(state)
    final = PostProcessNode()(
        {
            **state,
            **verified,
            "mode": "preflight",
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )

    assert verified["status"] == "error"
    assert "finding contains undeclared fields" in str(verified.get("error_log"))
    assert final["status"] == "error"
    assert final.get("formatted_output") is None
