from __future__ import annotations

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks


TRUSTED_SCOPE = {"production_company_id": "pc-1", "project_id": "pj-1"}
SESSION_ID = "session-1"


class Store:
    def __init__(self, value):
        self.value = value

    def resolve(self, ref, *, scope, session_id, consume):
        assert scope == TRUSTED_SCOPE
        assert session_id == SESSION_ID
        assert consume is True
        return self.value if ref == "ref-1" else None


def test_call_path_writes_valid_rows_and_returns_rejected_reasons():
    written = []

    def sanitize(_state, row):
        return {**row, "free_text": row["free_text"].replace("田中", "[worker]")}

    def normalize(_state, row):
        return {**row, "match_text": row["free_text"].casefold()}

    def write(_state, row):
        written.append(row)
        return f"id-{len(written)}"

    rows = [
        {"site_id": "S1", "observed_at": "2026-07-16", "free_text": "田中 inspected scaffold"},
        {"site_id": "S1", "free_text": "missing date"},
        {"site_id": "S1", "observed_at": "2026-07-16"},
    ]
    node = IngestRowPipelineNode(
        hooks=RowPipelineHooks(sanitize=sanitize, normalize=normalize, write=write),
    )

    result = node(
        {
            "rows": rows,
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "status": AgentStatus.PENDING.value,
        }
    )

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["ingest_summary"]["written_ids"] == ["id-1"]
    assert result["ingest_summary"]["rejected"] == [
        {"index": 1, "reasons": ["missing_observed_at"]},
        {"index": 2, "reasons": ["missing_free_text_or_category"]},
    ]
    assert written[0]["free_text"] == "[worker] inspected scaffold"
    assert written[0]["match_text"] == "[worker] inspected scaffold"


def test_write_failures_are_reported_per_row_without_silent_drop():
    def sanitize(_state, row):
        return row

    def normalize(_state, row):
        return row

    def write(_state, _row):
        raise RuntimeError("db unavailable")

    rows = [{"site_id": "S1", "observed_at": "2026-07-16", "category": "safe"}]
    node = IngestRowPipelineNode(
        hooks=RowPipelineHooks(sanitize=sanitize, normalize=normalize, write=write),
    )

    result = node.execute({"rows": rows})

    assert result["ingest_summary"]["written_ids"] == []
    assert result["ingest_summary"]["rejected_count"] == 1
    assert "db unavailable" in result["ingest_summary"]["rejected"][0]["reasons"][0]


def test_same_normalize_hook_is_the_storage_seam_for_retrieval_code():
    normalized_rows = []

    def sanitize(_state, row):
        return row

    def normalize(_state, row):
        normalized = {**row, "normalized_key": row["category"].casefold()}
        normalized_rows.append(normalized)
        return normalized

    def write(_state, row):
        assert row["normalized_key"] == "weather"
        return "id-1"

    node = IngestRowPipelineNode(
        hooks=RowPipelineHooks(sanitize=sanitize, normalize=normalize, write=write),
    )

    result = node.execute(
        {"rows": [{"site_id": "S1", "observed_at": "d", "category": "Weather"}]}
    )

    assert result["ingest_summary"]["written_ids"] == ["id-1"]
    assert normalized_rows[0]["normalized_key"] == "weather"


def test_write_hook_without_sanitize_hook_is_rejected_at_init():
    def normalize(_state, row):
        return row

    def write(_state, _row):
        return "id-1"

    with pytest.raises(ValueError, match="sanitize hook"):
        IngestRowPipelineNode(hooks=RowPipelineHooks(normalize=normalize, write=write))


def test_write_hook_without_normalize_hook_is_rejected_at_init():
    def sanitize(_state, row):
        return row

    def write(_state, _row):
        return "id-1"

    with pytest.raises(ValueError, match="normalize hook"):
        IngestRowPipelineNode(hooks=RowPipelineHooks(sanitize=sanitize, write=write))


def test_explicit_unsanitized_opt_out_is_required_and_visible():
    written = []

    def write(_state, row):
        written.append(row)
        return "id-1"

    with pytest.raises(ValueError, match="sanitize hook"):
        IngestRowPipelineNode(
            hooks=RowPipelineHooks(write=write),
            allow_unnormalized_writes=True,
        )

    node = IngestRowPipelineNode(
        hooks=RowPipelineHooks(write=write),
        allow_unsanitized_writes=True,
        allow_unnormalized_writes=True,
    )

    result = node.execute(
        {"rows": [{"site_id": "S1", "observed_at": "d", "category": "safe"}]}
    )

    assert result["ingest_summary"]["written_ids"] == ["id-1"]
    assert written[0]["category"] == "safe"


def test_payload_ref_unresolvable_fails_closed_instead_of_falling_back_to_state_rows():
    def sanitize(_state, row):
        return row

    def normalize(_state, row):
        return row

    def write(_state, _row):
        raise AssertionError("must not write fallback rows")

    node = IngestRowPipelineNode(
        payload_store=Store(None),
        trusted_scope=TRUSTED_SCOPE,
        session_id=SESSION_ID,
        hooks=RowPipelineHooks(sanitize=sanitize, normalize=normalize, write=write),
    )

    result = node.execute(
        {
            "payload_ref": "ref-1",
            "rows": [{"site_id": "S1", "observed_at": "d", "category": "safe"}],
        }
    )

    assert result["status"] == AgentStatus.ERROR.value
    assert "payload_ref unresolvable" in result["error_log"][0]


def test_required_field_allows_zero_and_false_but_rejects_empty_string_and_none():
    node = IngestRowPipelineNode(allow_unsanitized_writes=True, allow_unnormalized_writes=True)

    assert node.check_required_fields({"site_id": 0, "observed_at": False, "category": "safe"}, {}) == []
    assert node.check_required_fields({"site_id": "", "observed_at": None, "category": "safe"}, {}) == [
        "missing_site_id",
        "missing_observed_at",
    ]
