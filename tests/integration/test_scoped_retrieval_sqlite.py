"""R-1/R-5: real SQLite backend proves stored-scope retrieval behavior."""

from src.nodes.domain_nodes import LCRetrievalNode, _recall_query_terms, normalize_review_query
from src.scope import SCOPE_KEYS
from src.services import SQLiteKBBackend


def _production_query() -> str:
    """Build the query the way Stage1 does, not by hand.

    A hand-written query proves the backend can match something; it does not
    prove the string production actually sends can match anything. Those two
    diverged and Stage2 recalled nothing in production while this file stayed
    green.
    """
    return normalize_review_query(
        _recall_query_terms(
            {"insurance_certificate": False},
            {"amount": {"lc": 100.0, "doc": 120.0, "within_tolerance": False}},
            "on_time",
        )
    )


def _record(tenant: str, source_id: str, summary: str) -> dict:
    return {
        "source_id": source_id,
        "title": "review_point",
        "summary": summary,
        "snippet": "amount tolerance",
        "metadata": {
            "tenant_id": tenant,
            "record_type": "review_point",
            "quality_flag": "normal",
        },
    }


def test_real_sqlite_returns_same_partition_hit_and_hides_other_partition():
    backend = SQLiteKBBackend()
    backend.upsert(_record("tenant-a", "shared-id", "invoice amount mismatch"))
    backend.upsert(_record("tenant-b", "shared-id", "invoice amount mismatch"))
    node = LCRetrievalNode(
        scope_keys=SCOPE_KEYS,
        backend=backend,
        normalize_query=normalize_review_query,
        top_k=5,
    )

    result = node.execute({"tenant_id": "tenant-a", "query": _production_query()})

    assert result["status"] == "success"
    assert [hit["source_id"] for hit in result["retrieved_records"]] == ["shared-id"]
    assert result["retrieved_records"][0]["metadata"]["tenant_id"] == "tenant-a"
    assert result["retrieval_rejected"] == []


def test_real_sqlite_scope_filter_is_server_side_and_missing_scope_fails_closed():
    backend = SQLiteKBBackend()
    backend.upsert(_record("tenant-b", "tenant-b-only", "invoice amount mismatch"))
    node = LCRetrievalNode(
        scope_keys=SCOPE_KEYS,
        backend=backend,
        normalize_query=normalize_review_query,
        top_k=5,
    )

    empty = node.execute({"tenant_id": "tenant-a", "query": _production_query()})
    missing = node.execute({"query": "invoice amount"})

    assert empty["status"] == "success"
    assert empty["retrieved_records"] == []
    assert empty["degradation_reason"] == "no scoped retrieval hits"
    assert missing["status"] == "error"
    assert missing.get("retrieved_records") is None
