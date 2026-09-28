"""PB — payload-store scope/replay fail-closed (raw bodies never leak in-state)."""

from __future__ import annotations

import pytest

from src.payload_store.payload_store import PayloadStore, PayloadStoreError
from src.scope import SCOPE_KEYS, scope_for


def test_payload_ref_rejects_scope_mismatch_and_replay():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    ref = store.put("SENTINEL_RAW_LC_TEXT", scope=scope_for("tenant-a"), session_id="s")
    # cross-tenant read is refused
    with pytest.raises(PayloadStoreError, match="payload_ref_scope_mismatch"):
        store.resolve(ref, scope=scope_for("tenant-b"), session_id="s")
    # in-scope single-use read succeeds once
    assert store.resolve(ref, scope=scope_for("tenant-a"), session_id="s", consume=True) == "SENTINEL_RAW_LC_TEXT"
    # replay is refused
    with pytest.raises(PayloadStoreError, match="payload_ref_replayed"):
        store.resolve(ref, scope=scope_for("tenant-a"), session_id="s")


def test_put_requires_full_scope():
    store = PayloadStore(scope_keys=SCOPE_KEYS)
    with pytest.raises(PayloadStoreError, match="payload_ref_scope_missing"):
        store.put("x", scope={"tenant_id": "", "extra": "x"}, session_id="s")
