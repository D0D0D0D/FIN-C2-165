from __future__ import annotations

import json
from inspect import Parameter, signature
import sys
from pathlib import Path

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

PARTS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PARTS_ROOT / "payload-store" / "src"))

from payload_store import PayloadStore  # noqa: E402

from ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks  # noqa: E402


SCOPE_KEYS = ("production_company_id", "project_id")
TRUSTED_SCOPE = {"production_company_id": "pc-1", "project_id": "pj-1"}
SESSION_ID = "session-1"
VALID_ROW = {"site_id": "S1", "observed_at": "2026-08-12", "category": "safe"}


def _hooks(written: list[dict] | None = None) -> RowPipelineHooks:
    def sanitize(_state, row):
        return row

    def normalize(_state, row):
        return row

    def write(_state, row):
        if written is not None:
            written.append(row)
        return "row-1"

    return RowPipelineHooks(sanitize=sanitize, normalize=normalize, write=write)


def _node(
    store: PayloadStore,
    *,
    trusted_scope=TRUSTED_SCOPE,
    session_id=SESSION_ID,
    written: list[dict] | None = None,
) -> IngestRowPipelineNode:
    return IngestRowPipelineNode(
        payload_store=store,
        trusted_scope=trusted_scope,
        session_id=session_id,
        hooks=_hooks(written),
    )


def _put(store: PayloadStore, *, scope=TRUSTED_SCOPE, session_id=SESSION_ID) -> str:
    return store.put(
        json.dumps({"records": [VALID_ROW]}),
        scope=scope,
        session_id=session_id,
    )


def test_payload_store_instance_connects_without_adapter_and_resolves_ref():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)
    written: list[dict] = []

    result = _node(store, written=written)(
        {
            "payload_ref": ref,
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "status": AgentStatus.PENDING.value,
        }
    )

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["ingest_summary"]["written_ids"] == ["row-1"]
    assert written == [VALID_ROW]


def test_scope_mismatch_ref_is_not_resolved():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)
    wrong_scope = {**TRUSTED_SCOPE, "project_id": "pj-2"}

    result = _node(store, trusted_scope=wrong_scope).execute({"payload_ref": ref})

    assert result["status"] == AgentStatus.ERROR.value
    assert "payload_ref unresolvable" in result["error_log"][0]
    assert store.resolve(ref, scope=TRUSTED_SCOPE, session_id=SESSION_ID) is not None


def test_other_session_ref_is_not_resolved():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)

    result = _node(store, session_id="session-2").execute({"payload_ref": ref})

    assert result["status"] == AgentStatus.ERROR.value
    assert "payload_ref unresolvable" in result["error_log"][0]
    assert store.resolve(ref, scope=TRUSTED_SCOPE, session_id=SESSION_ID) is not None


def test_payload_ref_is_consumed_and_cannot_be_resolved_twice():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)
    node = _node(store)

    first = node.execute({"payload_ref": ref})
    second = node.execute({"payload_ref": ref})

    assert first["status"] == AgentStatus.SUCCESS.value
    assert second["status"] == AgentStatus.ERROR.value
    assert "payload_ref unresolvable" in second["error_log"][0]


def test_unresolvable_ref_never_falls_back_to_state_rows():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    written: list[dict] = []

    result = _node(store, written=written).execute(
        {
            "payload_ref": "unknown-ref",
            "rows": [VALID_ROW],
        }
    )

    assert result["status"] == AgentStatus.ERROR.value
    assert "payload_ref unresolvable" in result["error_log"][0]
    assert written == []


def test_state_scope_and_session_cannot_replace_explicit_trusted_context():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)
    node = IngestRowPipelineNode(payload_store=store, hooks=_hooks())
    forged_state = {
        "payload_ref": ref,
        **TRUSTED_SCOPE,
        "session_id": SESSION_ID,
    }

    with pytest.raises(ValueError, match="trusted_scope"):
        node.execute(forged_state)


def test_missing_session_id_is_a_wiring_error():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = _put(store)
    node = IngestRowPipelineNode(
        payload_store=store,
        trusted_scope=TRUSTED_SCOPE,
        hooks=_hooks(),
    )

    with pytest.raises(ValueError, match="session_id"):
        node.resolve_rows({"payload_ref": ref})


def test_public_payload_resolver_protocol_exposes_only_scoped_resolve():
    from ingest_row_pipeline import PayloadResolver

    assert "resolve" in PayloadResolver.__dict__
    assert "get" not in PayloadResolver.__dict__
    parameters = signature(PayloadResolver.resolve).parameters
    assert parameters["scope"].kind is Parameter.KEYWORD_ONLY
    assert parameters["session_id"].kind is Parameter.KEYWORD_ONLY
    assert parameters["consume"].default is True
