from __future__ import annotations

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from scoped_retrieval import ScopedRetrievalNode, normalize_retrieved_record


SCOPE_KEYS = ("production_company_id", "project_id")


class Backend:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, *, scope, query, top_k):
        self.calls.append({"scope": dict(scope), "query": query, "top_k": top_k})
        return self.hits


class MfgC2111StyleBackend:
    """Mirror an adapter that accepts scope but cannot pass it to its store."""

    def __init__(self, vector_store):
        self.vector_store = vector_store
        self.calls = []

    def search(self, *, scope, query, top_k):
        self.calls.append({"scope": dict(scope), "query": query, "top_k": top_k})
        return self.vector_store.search(query, top_k=top_k)


def normalize_query(value):
    if isinstance(value, dict):
        return "|".join(str(v).casefold() for v in value.values() if v)
    return str(value or "").casefold().strip()


def make_node(backend=None, **kwargs):
    kwargs.setdefault("scope_keys", SCOPE_KEYS)
    kwargs.setdefault("normalize_query", normalize_query)
    return ScopedRetrievalNode(backend=backend, **kwargs)


def scoped_state(**extra):
    state = {"production_company_id": "pc1", "project_id": "pj1"}
    state.update(extra)
    return state


def test_call_path_fails_closed_when_any_scope_key_missing_and_does_not_search():
    backend = Backend([{"source_id": "src_1"}])
    node = make_node(backend)

    result = node(
        {
            "production_company_id": "pc1",
            "query": "Q",
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )

    assert result["status"] == AgentStatus.ERROR.value
    assert "scope keys missing or invalid: ['project_id']" in result["error_log"][0]
    assert backend.calls == []


def test_fails_closed_when_all_scope_keys_missing():
    backend = Backend([{"source_id": "src_1"}])
    node = make_node(backend)

    result = node.execute({"query": "Q"})

    assert result["status"] == AgentStatus.ERROR.value
    assert "scope keys missing or invalid: ['production_company_id', 'project_id']" in result["error_log"][0]
    assert backend.calls == []


@pytest.mark.parametrize(
    "bad_value",
    ["", " ", "  \t ", "pj1 ", " pj1", "pj\n1", "pj\r1", 0, 1, False, True, {}, [], b"pj1", None],
)
def test_non_string_or_unclean_scope_values_fail_closed_and_do_not_search(bad_value):
    backend = Backend([{"source_id": "src_1"}])
    node = make_node(backend)

    result = node.execute(scoped_state(project_id=bad_value, query="Q"))

    assert result["status"] == AgentStatus.ERROR.value
    assert "project_id" in result["error_log"][0]
    assert backend.calls == []


def test_single_scope_key_domain_works():
    backend = Backend([{"source_id": "src_1"}])
    node = ScopedRetrievalNode(scope_keys=("case_id",), backend=backend, normalize_query=normalize_query)

    result = node.execute({"case_id": "c-9", "query": "q"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert backend.calls[0]["scope"] == {"case_id": "c-9"}


@pytest.mark.parametrize(
    "bad_keys",
    [(), "tenant_id", b"tenant_id", None, 42, {"tenant_id": "x"}, ("case_id", "case_id"),
     ("",), (" case_id",), ("case id",), ("case\nid",), (123,), (None,)],
)
def test_invalid_scope_keys_raise_value_error_at_construction(bad_keys):
    with pytest.raises(ValueError):
        ScopedRetrievalNode(scope_keys=bad_keys)


def test_normalizer_is_required_when_backend_is_configured():
    with pytest.raises(ValueError, match="normalize_query hook"):
        ScopedRetrievalNode(scope_keys=SCOPE_KEYS, backend=Backend([]))


def test_public_normalize_retrieved_record_rejects_hit_missing_expected_scope():
    result = normalize_retrieved_record(
        {"source_id": "cross_partition_1", "metadata": {"record_id": "cross_partition_1"}},
        expected_scope={"production_company_id": "pc1", "project_id": "pj1"},
    )

    assert result is None


def test_search_uses_scope_and_normalized_query_and_projects_safe_records():
    backend = Backend(
        [
            {
                "source_id": "src_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "score": 0.91,
                "source_tier": "internal_kb",
                "raw_narrative": "do not leak",
                "event_summary": "do not leak either",
                "metadata": {"record_id": "src_1", "raw_narrative": "secret", "category": "process"},
            }
        ]
    )
    node = make_node(backend, top_k=3)

    result = node.execute(scoped_state(query={"Category": "PROCESS"}))

    assert backend.calls == [
        {"scope": {"production_company_id": "pc1", "project_id": "pj1"}, "query": "process", "top_k": 3}
    ]
    assert result["status"] == AgentStatus.SUCCESS.value
    record = result["retrieved_records"][0]
    assert record["id"] == "src_1"
    assert record["source_id"] == "src_1"
    assert record["source_tier"] == "internal_kb"
    assert record["metadata"] == {
        "record_id": "src_1",
        "category": "process",
        "production_company_id": "pc1",
        "project_id": "pj1",
    }
    assert record["score"] == 0.91
    assert record["dropped_keys"] == ["event_summary", "raw_narrative"]
    assert "do not leak" not in str(result["retrieved_records"])
    assert "secret" not in str(result["retrieved_records"])


def test_metadata_scope_values_are_trusted_request_values_not_backend_copies():
    backend = Backend(
        [
            {
                "source_id": "src_1",
                "metadata": {"record_id": "src_1", "production_company_id": "pc1", "project_id": "pj1"},
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    metadata = result["retrieved_records"][0]["metadata"]
    assert metadata["production_company_id"] == "pc1"
    assert metadata["project_id"] == "pj1"
    assert "dropped_keys" not in result["retrieved_records"][0]


def test_dangerous_scope_key_name_does_not_leak_backend_metadata():
    # Even if a template declares a free-text field name as a scope key,
    # backend metadata under that name must never pass through: equal values
    # are replaced by the trusted request value, different values reject the hit.
    secret = "S" * 200
    backend = Backend(
        [
            {"source_id": "src_1", "metadata": {"record_id": "src_1", "raw_narrative": secret}},
            {"source_id": "src_2", "metadata": {"record_id": "src_2", "raw_narrative": "rn1"}},
        ]
    )
    node = ScopedRetrievalNode(scope_keys=("raw_narrative",), backend=backend, normalize_query=normalize_query)

    result = node.execute({"raw_narrative": "rn1", "query": "q"})

    assert secret not in str(result)
    assert result["retrieval_rejected"] == [{"index": 0, "reason": "scope mismatch: ['raw_narrative']"}]
    assert result["retrieved_records"][0]["metadata"]["raw_narrative"] == "rn1"


def test_hits_with_mismatched_scope_are_rejected_without_value_echo():
    backend = Backend(
        [
            {
                "source_id": "leak_1",
                "metadata": {
                    "record_id": "leak_1",
                    "production_company_id": "pc1",
                    "project_id": "OTHER-PROJECT",
                },
            },
            {
                "source_id": "leak_2",
                "production_company_id": "pc1",
                "project_id": "OTHER-PROJECT",
                "metadata": {"record_id": "leak_2"},
            },
            {
                "source_id": "ok_1",
                "metadata": {
                    "record_id": "ok_1",
                    "production_company_id": "pc1",
                    "project_id": "pj1",
                },
            },
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert [r["source_id"] for r in result["retrieved_records"]] == ["ok_1"]
    assert result["retrieval_rejected"] == [
        {"index": 0, "reason": "scope mismatch: ['project_id']"},
        {"index": 1, "reason": "scope mismatch: ['project_id']"},
    ]
    assert "OTHER-PROJECT" not in str(result)


def test_hit_with_all_declared_scope_keys_missing_is_rejected():
    backend = Backend([{"source_id": "cross_partition_1", "metadata": {"record_id": "cross_partition_1"}}])
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"] == [
        {"index": 0, "reason": "scope mismatch: ['production_company_id', 'project_id']"}
    ]


def test_non_dict_hits_are_rejected_by_scope_gate_and_degrade():
    node = make_node(Backend([None, "raw-hit", ["nested-hit"]]))

    result = node.execute(scoped_state(query="q"))

    assert result == {
        "retrieved_records": [],
        "retrieval_rejected": [
            {"index": 0, "reason": "scope mismatch: ['production_company_id', 'project_id']"},
            {"index": 1, "reason": "scope mismatch: ['production_company_id', 'project_id']"},
            {"index": 2, "reason": "scope mismatch: ['production_company_id', 'project_id']"},
        ],
        "status": AgentStatus.SUCCESS.value,
        "degradation_reason": "no scoped retrieval hits",
    }


def test_hit_with_only_some_declared_scope_keys_is_rejected():
    backend = Backend(
        [
            {
                "source_id": "cross_partition_1",
                "production_company_id": "pc1",
                "metadata": {"record_id": "cross_partition_1"},
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"] == [{"index": 0, "reason": "scope mismatch: ['project_id']"}]


def test_hit_with_conflicting_top_level_and_metadata_scope_is_rejected():
    backend = Backend(
        [
            {
                "source_id": "cross_partition_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "metadata": {
                    "record_id": "cross_partition_1",
                    "production_company_id": "OTHER-COMPANY",
                    "project_id": "pj1",
                },
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"] == [
        {"index": 0, "reason": "scope mismatch: ['production_company_id']"}
    ]
    assert "OTHER-COMPANY" not in str(result)


def test_hit_with_wrong_top_level_and_matching_metadata_scope_is_rejected():
    backend = Backend(
        [
            {
                "source_id": "cross_partition_1",
                "production_company_id": "OTHER-COMPANY",
                "project_id": "pj1",
                "metadata": {
                    "record_id": "cross_partition_1",
                    "production_company_id": "pc1",
                    "project_id": "pj1",
                },
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"] == [
        {"index": 0, "reason": "scope mismatch: ['production_company_id']"}
    ]
    assert "OTHER-COMPANY" not in str(result)


def test_hit_with_matching_top_level_and_metadata_scope_is_adopted():
    backend = Backend(
        [
            {
                "source_id": "scoped_both_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "metadata": {
                    "record_id": "scoped_both_1",
                    "production_company_id": "pc1",
                    "project_id": "pj1",
                },
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert [record["source_id"] for record in result["retrieved_records"]] == ["scoped_both_1"]
    assert result["retrieval_rejected"] == []
    assert result["retrieved_records"][0]["metadata"] == {
        "record_id": "scoped_both_1",
        "production_company_id": "pc1",
        "project_id": "pj1",
    }


def test_hit_with_all_declared_scope_keys_matching_is_adopted_with_trusted_metadata():
    backend = Backend(
        [
            {
                "source_id": "scoped_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "metadata": {"record_id": "scoped_1"},
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert [record["source_id"] for record in result["retrieved_records"]] == ["scoped_1"]
    assert result["retrieval_rejected"] == []
    assert result["retrieved_records"][0]["metadata"] == {
        "record_id": "scoped_1",
        "production_company_id": "pc1",
        "project_id": "pj1",
    }


def test_mfg_c2_111_style_backend_rejects_all_unscoped_cross_partition_hits():
    class UnscopedVectorStore:
        def search(self, query, *, top_k):
            return [
                {"source_id": "partition_b_1", "metadata": {"record_id": "partition_b_1"}},
                {"source_id": "partition_b_2", "metadata": {"record_id": "partition_b_2"}},
            ][:top_k]

    backend = MfgC2111StyleBackend(UnscopedVectorStore())
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    assert backend.calls == [
        {"scope": {"production_company_id": "pc1", "project_id": "pj1"}, "query": "q", "top_k": 8}
    ]
    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"] == [
        {"index": 0, "reason": "scope mismatch: ['production_company_id', 'project_id']"},
        {"index": 1, "reason": "scope mismatch: ['production_company_id', 'project_id']"},
    ]
    assert result["degradation_reason"] == "no scoped retrieval hits"
    assert result["status"] == AgentStatus.SUCCESS.value


def test_projection_drops_non_allowlisted_description_without_value_echo():
    long_description = "田中太郎が転倒した詳細な自由記述 " * 10
    backend = Backend(
        [
            {
                "source_id": "src_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "description": long_description,
                "note": "also raw",
                "metadata": {"record_id": "src_1", "category": "process"},
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    record = result["retrieved_records"][0]
    assert "description" not in record
    assert "description" not in record["metadata"]
    assert "note" not in record["metadata"]
    assert long_description not in str(result)
    assert sorted(record["dropped_keys"]) == ["description", "note"]


def test_projection_keeps_allowlisted_metadata_with_shape_guard():
    backend = Backend(
        [
            {
                "source_id": "src_1",
                "production_company_id": "pc1",
                "project_id": "pj1",
                "metadata": {
                    "record_id": "REC-1",
                    "category": "process",
                    "source_doc": "SOP-1",
                    "chunk_hash": "abc123",
                    "remarks": "田中太郎が転倒した詳細な自由記述 " * 10,
                    "url": "https://x\nevil",
                    "title": "T" * 100,
                },
            }
        ]
    )
    node = make_node(backend)

    result = node.execute(scoped_state(query="q"))

    metadata = result["retrieved_records"][0]["metadata"]
    assert metadata == {
        "record_id": "REC-1",
        "category": "process",
        "source_doc": "SOP-1",
        "chunk_hash": "abc123",
        "production_company_id": "pc1",
        "project_id": "pj1",
    }
    assert "remarks" in result["retrieved_records"][0]["dropped_keys"]
    assert "url" in result["retrieved_records"][0]["dropped_keys"]
    assert "title" in result["retrieved_records"][0]["dropped_keys"]


def test_empty_hits_are_successful_degradation_not_system_error():
    node = make_node(Backend([]))

    result = node.execute(scoped_state(query="no match"))

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["retrieved_records"] == []
    assert result["degradation_reason"] == "no scoped retrieval hits"


def test_missing_ids_are_rejected_and_degrade_when_no_valid_records():
    node = make_node(
        Backend([{"production_company_id": "pc1", "project_id": "pj1", "summary": "missing id"}])
    )

    result = node.execute(scoped_state(query="q"))

    assert result["retrieved_records"] == []
    assert result["retrieval_rejected"][0]["reason"] == "missing id/source_id"
    assert result["degradation_reason"] == "no scoped retrieval hits"
