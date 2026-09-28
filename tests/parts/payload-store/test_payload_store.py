from __future__ import annotations

import pytest

from payload_store import PayloadStore, PayloadStoreError, is_reference, mint_reference


SCOPE_KEYS = ("production_company_id", "project_id")


def _scope(**overrides: str) -> dict[str, str]:
    scope = {"production_company_id": "pc-1", "project_id": "pj-1"}
    scope.update(overrides)
    return scope


def make_store(**kwargs) -> PayloadStore:
    kwargs.setdefault("scope_keys", SCOPE_KEYS)
    return PayloadStore(**kwargs)


def test_resolve_roundtrip_then_consume_rejects_replay():
    store = make_store()
    payload = {"rows": [{"id": "row-1"}]}
    ref = store.put(payload, scope=_scope(), session_id="session-1")

    assert store.resolve(ref, scope=_scope(), session_id="session-1") == payload
    assert store.resolve(ref, scope=_scope(), session_id="session-1", consume=True) == payload
    with pytest.raises(PayloadStoreError, match="^payload_ref_replayed$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


@pytest.mark.parametrize(
    ("wrong_scope", "wrong_session"),
    [
        (_scope(production_company_id="pc-2"), "session-1"),
        (_scope(project_id="pj-2"), "session-1"),
        (_scope(), "session-2"),
    ],
)
def test_resolve_rejects_scope_mismatch_on_each_axis(wrong_scope, wrong_session):
    store = make_store()
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(ref, scope=wrong_scope, session_id=wrong_session)


def test_single_scope_key_store_works_and_isolates():
    store = PayloadStore(scope_keys=("case_id",))
    ref = store.put({"body": "private"}, scope={"case_id": "c-1"}, session_id="s-1")

    assert store.resolve(ref, scope={"case_id": "c-1"}, session_id="s-1") == {"body": "private"}
    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(ref, scope={"case_id": "c-2"}, session_id="s-1")


@pytest.mark.parametrize(
    "bad_keys",
    [(), "tenant_id", b"tenant_id", None, 42, {"tenant_id": "x"}, ("case_id", "case_id"),
     ("",), (" case_id",), ("case id",), (123,), (None,)],
)
def test_invalid_scope_keys_raise_value_error_at_construction(bad_keys):
    with pytest.raises(ValueError):
        PayloadStore(scope_keys=bad_keys)


def test_scope_mismatch_does_not_consume_payload():
    store = make_store()
    payload = {"body": "private"}
    ref = store.put(payload, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(ref, scope=_scope(production_company_id="pc-2"), session_id="session-1", consume=True)

    assert store.resolve(ref, scope=_scope(), session_id="session-1", consume=True) == payload


def test_expired_ref_is_dropped_using_injected_clock():
    now = [100.0]
    store = make_store(ttl_seconds=10, clock=lambda: now[0])
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")
    now[0] = 111.0

    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


def test_ref_expires_at_exact_ttl_boundary():
    now = [100.0]
    store = make_store(ttl_seconds=10, clock=lambda: now[0])
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")
    now[0] = 110.0

    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


def test_non_positive_ttl_is_immediately_expired():
    now = [100.0]
    store = make_store(ttl_seconds=0, clock=lambda: now[0])
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


@pytest.mark.parametrize("ref", [None, "unknown-ref"])
def test_unknown_or_missing_ref_is_rejected(ref):
    store = make_store()

    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


@pytest.mark.parametrize(
    ("scope", "session_id"),
    [
        (_scope(production_company_id=""), "session-1"),
        (_scope(project_id=""), "session-1"),
        (_scope(), ""),
        (_scope(production_company_id=" "), "session-1"),
        (_scope(project_id="pj-1 "), "session-1"),
        (_scope(project_id="pj\n1"), "session-1"),
        ({"production_company_id": "pc-1"}, "session-1"),  # declared key missing
        ({**_scope(), "extra_key": "x"}, "session-1"),  # undeclared extra key
        (None, "session-1"),
        ("pc-1", "session-1"),
        (_scope(project_id=0), "session-1"),
        (_scope(project_id=False), "session-1"),
        (_scope(), None),
        (_scope(), 5),
    ],
)
def test_put_rejects_missing_or_invalid_scope(scope, session_id):
    store = make_store()

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_missing$"):
        store.put({"body": "private"}, scope=scope, session_id=session_id)


def test_resolve_and_delete_reject_malformed_scope_before_touching_items():
    store = make_store()
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_missing$"):
        store.resolve(ref, scope={"production_company_id": "pc-1"}, session_id="session-1")
    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_missing$"):
        store.delete(ref, scope=None, session_id="session-1")

    assert store.resolve(ref, scope=_scope(), session_id="session-1") == {"body": "private"}


def test_delete_is_idempotent():
    store = make_store()
    ref = store.put({"body": "private"}, scope=_scope(), session_id="session-1")

    store.delete(ref, scope=_scope(), session_id="session-1")
    store.delete(ref, scope=_scope(), session_id="session-1")
    store.delete(None, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


def test_cross_scope_delete_is_rejected_without_destroying_payload():
    store = make_store()
    payload = {"body": "private"}
    ref = store.put(payload, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.delete(ref, scope=_scope(project_id="pj-2"), session_id="session-1")

    assert store.resolve(ref, scope=_scope(), session_id="session-1") == payload


def test_store_has_no_unscoped_read_api():
    store = make_store()

    assert not hasattr(store, "get")


def test_consume_tombstones_the_raw_value_in_memory():
    """After a single-use consume the raw body must not stay resident.

    A forgotten delete() previously left the confidential payload in _items
    until process exit. The tombstone drops item.value while keeping the item
    so replay is still fail-closed.
    """
    store = make_store()
    payload = {"body": "confidential-script-marker"}
    ref = store.put(payload, scope=_scope(), session_id="session-1")

    assert store.resolve(ref, scope=_scope(), session_id="session-1", consume=True) == payload

    # The stored item is a tombstone: still present (so replay fail-closes) but
    # its raw value is gone.
    stored = store._items[ref]
    assert stored.value is None
    assert stored.used is True

    # Replay is still rejected as replayed, not unknown.
    with pytest.raises(PayloadStoreError, match="^payload_ref_replayed$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


def test_scope_mismatch_consume_does_not_tombstone_value():
    """A rejected (wrong-scope) resolve must not drop the legitimate value."""
    store = make_store()
    payload = {"body": "private"}
    ref = store.put(payload, scope=_scope(), session_id="session-1")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(ref, scope=_scope(production_company_id="pc-2"), session_id="session-1", consume=True)

    # Value survived the rejected attempt and a legitimate consume still works.
    assert store._items[ref].value == payload
    assert store.resolve(ref, scope=_scope(), session_id="session-1", consume=True) == payload


def test_expired_but_unreferenced_payload_is_swept_on_next_put():
    """An expired ref that is never resolved again must not linger in memory.

    This is the crash-before-delete() case: the caller put a payload, then died
    before its finally: delete(). Without the sweep the raw body would stay in
    _items until process exit despite the TTL. A later, unrelated put() must
    evict it.
    """
    now = [100.0]
    store = make_store(ttl_seconds=10, clock=lambda: now[0])
    orphan = store.put({"body": "orphaned-secret"}, scope=_scope(), session_id="session-1")
    assert orphan in store._items

    now[0] = 111.0  # orphan is now past TTL, but nobody resolves it

    # An unrelated put on the same store sweeps the expired orphan.
    other = store.put({"body": "fresh"}, scope=_scope(), session_id="session-2")
    assert orphan not in store._items
    assert other in store._items


def test_resolve_sweeps_other_expired_items_but_keeps_own_expiry_semantics():
    """resolve() sweeps siblings, yet still reports the target's own expiry.

    The swept sibling must be gone, while resolving the (also expired) target
    ref must still raise payload_ref_expired — not payload_ref_unknown — so the
    error contract that distinguishes expiry from absence is preserved.
    """
    now = [100.0]
    store = make_store(ttl_seconds=10, clock=lambda: now[0])
    target = store.put({"body": "target"}, scope=_scope(), session_id="session-1")
    sibling = store.put({"body": "sibling"}, scope=_scope(), session_id="session-2")

    now[0] = 111.0  # both expired

    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(target, scope=_scope(), session_id="session-1")
    # The sibling was swept as a side effect.
    assert sibling not in store._items


def test_tombstone_is_swept_after_ttl_and_stays_fail_closed():
    """A consume tombstone must not accumulate forever, and stays fail-closed.

    After consume the item is a tombstone (value=None). Once its TTL passes it
    should be swept like any expired item, and a later resolve must still
    fail-closed (expired if it is the kept target, unknown once swept) — never
    return the (already None) value.
    """
    now = [100.0]
    store = make_store(ttl_seconds=10, clock=lambda: now[0])
    ref = store.put({"body": "secret"}, scope=_scope(), session_id="session-1")
    store.resolve(ref, scope=_scope(), session_id="session-1", consume=True)  # tombstone
    assert store._items[ref].value is None

    now[0] = 111.0  # tombstone now past TTL

    # Resolving the (kept) target still fail-closes as expired, not a value.
    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")
    # And it is gone afterwards.
    assert ref not in store._items


def test_canonical_flow_resolve_consume_then_delete_leaves_nothing():
    """USAGE canonical flow: resolve(consume=True) then finally: delete()."""
    store = make_store()
    payload = {"rows": [{"id": "row-1"}]}
    ref = store.put(payload, scope=_scope(), session_id="session-1")
    try:
        assert store.resolve(ref, scope=_scope(), session_id="session-1", consume=True) == payload
    finally:
        store.delete(ref, scope=_scope(), session_id="session-1")

    # Nothing left in the store, and the ref is now unknown (fail-closed).
    assert ref not in store._items
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        store.resolve(ref, scope=_scope(), session_id="session-1")


# --- Reference format (v0.4.0) ----------------------------------------------
#
# References must be lexically disjoint from PII patterns.  `uuid4` text is not:
# the S-2 input gate masks anything `detect_pii` matches, and ~0.58% of uuid4
# strings match phone_jp / credit_card / my_number_jp, which silently rewrote the
# reference inside the canonical envelope and broke resolve() at that rate.


def test_reference_alphabet_is_exactly_a_to_p_and_digit_free():
    # Pin the alphabet itself: `isalpha()` alone would still pass for upper-case
    # or non-ASCII letters, and upper case would re-open the `name` pattern.
    allowed = set("abcdefghijklmnop")
    for _ in range(2000):
        ref = mint_reference()
        assert set(ref) <= allowed, ref
        assert len(ref) == 32, ref
        assert is_reference(ref)


def test_reference_encoding_is_injective_over_its_byte_space():
    # Decode letter->nibble->byte and confirm the mapping loses nothing, so
    # collision resistance is exactly that of the 16 random bytes behind it.
    alphabet = "abcdefghijklmnop"
    seen_bytes = set()
    for _ in range(5000):
        ref = mint_reference()
        nibbles = [alphabet.index(ch) for ch in ref]
        raw = bytes((hi << 4) | lo for hi, lo in zip(nibbles[::2], nibbles[1::2]))
        assert len(raw) == 16
        seen_bytes.add(raw)
    assert len(seen_bytes) == 5000          # distinct refs -> distinct bytes


def test_reference_has_no_fixed_position_fingerprint():
    # uuid4 would pin version/variant nibbles to constants; CSPRNG bytes do not.
    for position in (12, 16):
        observed = {mint_reference()[position] for _ in range(3000)}
        assert len(observed) > 1, position


def test_store_mints_pii_disjoint_references():
    store = make_store()
    ref = store.put({"body": "x"}, scope=_scope(), session_id="session-1")
    assert ref.isalpha()
    assert store.resolve(ref, scope=_scope(), session_id="session-1") == {"body": "x"}


def test_reference_is_not_matched_by_the_framework_pii_detector():
    """The regression this format exists to prevent, asserted end-to-end."""

    try:
        from shared.security.pii_detector import detect_pii
    except ImportError:  # part tests may run without the SDK installed
        pytest.skip("framework detect_pii unavailable")

    for _ in range(5000):
        assert detect_pii(mint_reference()) == []

    # The real failure path was the reference embedded in the canonical
    # envelope JSON, not the bare string -- assert that shape too.
    import json

    for _ in range(3000):
        envelope = json.dumps(
            {"mode": "invoke", "tenant_id": "t1", "case_ref": "c1", "payload_ref": mint_reference()},
            sort_keys=True,
        )
        assert detect_pii(envelope) == []
