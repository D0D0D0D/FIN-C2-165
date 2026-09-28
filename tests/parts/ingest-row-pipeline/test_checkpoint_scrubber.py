from __future__ import annotations

import json

import pytest

import ingest_row_pipeline
from ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from ingest_row_pipeline.checkpoint_scrubber import (
    scrub_checkpoint_envelope as module_scrub_checkpoint_envelope,
)


class Store:
    def __init__(self):
        self.values = {}
        self.bindings = {}
        self.count = 0

    def put(self, value: str, *, scope, session_id) -> str:
        self.count += 1
        ref = f"server-ref-{self.count}"
        self.values[ref] = value
        self.bindings[ref] = {"scope": dict(scope), "session_id": session_id}
        return ref


def test_client_payload_ref_is_discarded_and_server_ref_is_always_reissued():
    store = Store()
    raw = json.dumps(
        {
            "mode": "ingest",
            "payload_ref": "client-forged-ref",
            "payload": {"records": [{"site_id": "S1", "free_text": "worker note"}]},
        }
    )

    safe = json.loads(scrub_checkpoint_envelope(raw, store, session_id="s-1"))

    assert safe["payload_ref"] == "server-ref-1"
    assert safe["payload_ref"] != "client-forged-ref"
    assert json.loads(store.values["server-ref-1"])["records"][0]["free_text"] == "worker note"


def test_unknown_string_keys_are_scrubbed_by_allowlist_not_left_in_checkpoint():
    store = Store()
    raw = json.dumps({"mode": "ingest", "payload": {"metadata": {"free_text": "田中太郎 090-1111"}}})

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1")
    safe = json.loads(safe_text)

    assert "田中太郎" not in safe_text
    assert safe["payload"]["metadata"]["free_text"] == "[scrubbed:payload.metadata.free_text]"
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"]["payload.metadata.free_text"] == "田中太郎 090-1111"


def test_unparsable_input_is_not_passed_through_to_checkpoint():
    store = Store()
    safe_text = scrub_checkpoint_envelope("田中太郎が転倒した", store, session_id="s-1")
    safe = json.loads(safe_text)

    assert "田中太郎" not in safe_text
    assert safe["__unparsable__"] is True
    assert json.loads(store.values[safe["payload_ref"]])["__raw_input__"] == "田中太郎が転倒した"


def test_tags_are_not_default_safe_scalar_list_values():
    store = Store()
    raw = json.dumps({"mode": "ingest", "payload": {"tags": ["田中太郎が転倒", "safe-code"]}})

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1")
    safe = json.loads(safe_text)

    assert "田中太郎" not in safe_text
    assert safe["payload"]["tags"][0] == "[scrubbed:payload.tags[0]]"
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"]["payload.tags[0]"] == "田中太郎が転倒"


def test_safe_scalar_list_values_use_shape_guard():
    store = Store()
    unsafe_source_id = "SRC-" + ("田中太郎が左膝を負傷した" * 8)
    raw = json.dumps({"mode": "ingest", "payload": {"source_ids": ["SRC-1", unsafe_source_id]}})

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1")
    safe = json.loads(safe_text)

    assert safe["payload"]["source_ids"][0] == "SRC-1"
    assert safe["payload"]["source_ids"][1] == "[scrubbed:payload.source_ids[1]]"
    assert unsafe_source_id not in safe_text


def test_safe_key_string_values_use_shape_guard_and_are_stashed_when_too_long_or_multiline():
    store = Store()
    long_record_id = "田中太郎は左膝を負傷したため現場で詳細な聞き取りが必要です" * 4
    raw = json.dumps({"mode": "ingest", "record_id": long_record_id})

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1")
    safe = json.loads(safe_text)

    assert safe["record_id"] == "[scrubbed:record_id]"
    assert long_record_id not in safe_text
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"]["record_id"] == long_record_id


SCOPE_POLICY = CheckpointScrubPolicy(scope_keys=frozenset({"production_company_id", "project_id"}))
TRUSTED_SCOPE = {"production_company_id": "pc-1", "project_id": "pj-1"}


def test_undeclared_scope_keys_are_scrubbed_by_default():
    """tenant_id/org_id are no longer baked into the default allowlist —
    client-sent partition keys never survive without a declaration."""
    store = Store()
    raw = json.dumps({"mode": "ingest", "tenant_id": "pc-1", "org_id": "pj-1", "site_id": "S1"})

    safe = json.loads(scrub_checkpoint_envelope(raw, store, session_id="s-1"))

    assert safe["tenant_id"] == "[scrubbed:tenant_id]"
    assert safe["org_id"] == "[scrubbed:org_id]"
    assert safe["site_id"] == "[scrubbed:site_id]"


def test_forged_client_scope_is_replaced_by_trusted_values():
    """Client-sent scope values never survive — even short clean ones are
    scrubbed and overwritten with the verified per-request values."""
    store = Store()
    raw = json.dumps({"mode": "ingest", "production_company_id": "attacker-pc", "project_id": "other-project"})

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1", trusted_scope=TRUSTED_SCOPE, policy=SCOPE_POLICY)
    safe = json.loads(safe_text)

    assert safe["production_company_id"] == "pc-1"
    assert safe["project_id"] == "pj-1"
    assert "attacker-pc" not in safe_text
    assert "other-project" not in safe_text
    # The stashed raw payload is bound to the trusted scope + session.
    assert store.bindings[safe["payload_ref"]] == {"scope": TRUSTED_SCOPE, "session_id": "s-1"}


def test_stash_binding_without_declared_scope_uses_empty_scope_and_session():
    store = Store()

    safe = json.loads(scrub_checkpoint_envelope(json.dumps({"mode": "ingest"}), store, session_id="s-9"))

    assert store.bindings[safe["payload_ref"]] == {"scope": {}, "session_id": "s-9"}


@pytest.mark.parametrize("bad_session", ["", " ", "s ", "s\n1", None, 7])
def test_invalid_session_id_is_rejected_before_any_stash(bad_session):
    store = Store()

    with pytest.raises(ValueError, match="session_id"):
        scrub_checkpoint_envelope(json.dumps({"mode": "ingest"}), store, session_id=bad_session)

    assert store.values == {}


def test_unparsable_stash_is_also_scope_and_session_bound():
    store = Store()

    safe = json.loads(
        scrub_checkpoint_envelope(
            "田中太郎が転倒した", store, session_id="s-1", trusted_scope=TRUSTED_SCOPE, policy=SCOPE_POLICY
        )
    )

    assert store.bindings[safe["payload_ref"]] == {"scope": TRUSTED_SCOPE, "session_id": "s-1"}


def test_nested_scope_named_keys_are_scrubbed_not_allowlisted():
    """A scope-named key inside payload/metadata is arbitrary client content —
    it must be stashed, never kept in the checkpoint."""
    store = Store()
    raw = json.dumps(
        {
            "mode": "ingest",
            "payload": {"metadata": {"project_id": "田中太郎の自由記述をここに隠す"}},
        }
    )

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1", trusted_scope=TRUSTED_SCOPE, policy=SCOPE_POLICY)
    safe = json.loads(safe_text)

    assert "田中太郎" not in safe_text
    assert safe["payload"]["metadata"]["project_id"] == "[scrubbed:payload.metadata.project_id]"
    # Top-level scope is the trusted injection.
    assert safe["project_id"] == "pj-1"


def test_scope_markers_can_never_reach_top_level_scope_values():
    """Long/multiline client scope values become markers in nested paths, but
    the top-level scope in the output is always the trusted dict — a marker
    can never flow into retrieval as a scope value."""
    store = Store()
    raw = json.dumps({"mode": "ingest", "project_id": "PJ\n" + "x" * 200})

    safe = json.loads(scrub_checkpoint_envelope(raw, store, session_id="s-1", trusted_scope=TRUSTED_SCOPE, policy=SCOPE_POLICY))

    assert safe["project_id"] == "pj-1"
    assert safe["production_company_id"] == "pc-1"


def test_unparsable_input_still_carries_trusted_scope():
    store = Store()

    safe = json.loads(
        scrub_checkpoint_envelope("田中太郎が転倒した", store, session_id="s-1", trusted_scope=TRUSTED_SCOPE, policy=SCOPE_POLICY)
    )

    assert safe["__unparsable__"] is True
    assert safe["production_company_id"] == "pc-1"
    assert safe["project_id"] == "pj-1"


@pytest.mark.parametrize(
    "bad_scope",
    [
        None,
        {},
        {"production_company_id": "pc-1"},  # declared key missing
        {**TRUSTED_SCOPE, "extra": "x"},  # undeclared extra key
        {"production_company_id": "pc-1", "project_id": ""},
        {"production_company_id": "pc-1", "project_id": " pj-1"},
        {"production_company_id": "pc-1", "project_id": "pj\n1"},
        {"production_company_id": "pc-1", "project_id": 7},
        ("pc-1", "pj-1"),
    ],
)
def test_declared_scope_keys_require_a_valid_trusted_scope(bad_scope):
    store = Store()

    with pytest.raises(ValueError):
        scrub_checkpoint_envelope(json.dumps({"mode": "ingest"}), store, session_id="s-1", trusted_scope=bad_scope, policy=SCOPE_POLICY)


def test_trusted_scope_without_declared_scope_keys_is_rejected():
    store = Store()

    with pytest.raises(ValueError, match="scope_keys is empty"):
        scrub_checkpoint_envelope(json.dumps({"mode": "ingest"}), store, session_id="s-1", trusted_scope=TRUSTED_SCOPE)


def test_reserved_or_invalid_scope_key_declarations_are_rejected():
    store = Store()

    for bad_key in ("payload_ref", "payload", "mode", "op", "status", "__unparsable__", "not identifier"):
        policy = CheckpointScrubPolicy(scope_keys=frozenset({bad_key}))
        with pytest.raises(ValueError, match="invalid scope key declaration"):
            scrub_checkpoint_envelope(
                json.dumps({"mode": "ingest"}), store, session_id="s-1", trusted_scope={bad_key: "v"}, policy=policy
            )


def test_safe_string_shape_limit_is_policy_configurable():
    store = Store()
    policy = CheckpointScrubPolicy(max_safe_string_chars=4)
    safe = json.loads(scrub_checkpoint_envelope(json.dumps({"record_id": "ABCDE"}), store, session_id="s-1", policy=policy))

    assert safe["record_id"] == "[scrubbed:record_id]"


@pytest.mark.parametrize(
    "scrubber",
    [
        ingest_row_pipeline.scrub_checkpoint_envelope,
        module_scrub_checkpoint_envelope,
    ],
    ids=["package-export", "module-export"],
)
def test_unknown_non_string_scalars_are_stashed_and_replaced_through_public_exports(scrubber):
    store = Store()
    unknown_scalars = {
        "phone_as_number": 9012345678,
        "score": 12.5,
        "is_vip": True,
        "nothing": None,
    }

    safe = json.loads(
        scrubber(
            json.dumps({"mode": "ingest", "unknown_key": unknown_scalars}),
            store,
            session_id="s-1",
        )
    )

    assert safe["unknown_key"] == {
        key: f"[scrubbed:unknown_key.{key}]" for key in unknown_scalars
    }
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"] == {
        f"unknown_key.{key}": value for key, value in unknown_scalars.items()
    }


@pytest.mark.parametrize("mode", ["ingest", "invoke", "feedback"])
def test_numeric_pii_sentinel_is_absent_from_entire_checkpoint_envelope(mode):
    store = Store()
    numeric_pii_sentinel = 9012345678
    raw = json.dumps(
        {
            "mode": mode,
            "unknown_key": {"phone_as_number": numeric_pii_sentinel},
        }
    )

    safe_text = scrub_checkpoint_envelope(raw, store, session_id="s-1")
    safe = json.loads(safe_text)

    assert str(numeric_pii_sentinel) not in safe_text
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"]["unknown_key.phone_as_number"] == numeric_pii_sentinel


def test_allowlisted_non_string_scalars_and_safe_scalar_list_items_remain_typed():
    store = Store()
    raw = json.dumps(
        {
            "record_id": 100123456,
            "observed_at": 12.5,
            "category": True,
            "source_record_id": None,
            "source_ids": [100123456, 12.5, True, None],
        }
    )

    safe = json.loads(scrub_checkpoint_envelope(raw, store, session_id="s-1"))

    assert safe["record_id"] == 100123456
    assert safe["observed_at"] == 12.5
    assert safe["category"] is True
    assert safe["source_record_id"] is None
    assert safe["source_ids"] == [100123456, 12.5, True, None]


def test_safe_subtree_and_valid_enum_are_preserved_but_invalid_enum_scalar_is_stashed():
    store = Store()
    policy = CheckpointScrubPolicy(safe_subtree_keys=frozenset({"safe_metadata"}))
    safe_metadata = {
        "phone_as_number": 9012345678,
        "score": 12.5,
        "is_vip": True,
        "nothing": None,
    }
    raw = json.dumps(
        {
            "mode": "ingest",
            "safe_metadata": safe_metadata,
            "nested": {"op": 100123456},
        }
    )

    safe = json.loads(scrub_checkpoint_envelope(raw, store, session_id="s-1", policy=policy))

    assert safe["mode"] == "ingest"
    assert safe["safe_metadata"] == safe_metadata
    assert safe["nested"]["op"] == "[scrubbed:nested.op]"
    stored = json.loads(store.values[safe["payload_ref"]])
    assert stored["__extra__"]["nested.op"] == 100123456


def test_server_payload_ref_and_trusted_scope_survive_non_string_scalar_scrubbing():
    store = Store()
    raw = json.dumps(
        {
            "mode": "ingest",
            "payload_ref": "client-forged-ref",
            "production_company_id": 100123456,
            "project_id": False,
            "unknown_key": 9012345678,
        }
    )

    safe_text = scrub_checkpoint_envelope(
        raw,
        store,
        session_id="s-1",
        trusted_scope=TRUSTED_SCOPE,
        policy=SCOPE_POLICY,
    )
    safe = json.loads(safe_text)

    assert safe["payload_ref"] == "server-ref-1"
    assert safe["payload_ref"] != "client-forged-ref"
    assert safe["production_company_id"] == TRUSTED_SCOPE["production_company_id"]
    assert safe["project_id"] == TRUSTED_SCOPE["project_id"]
    assert safe["unknown_key"] == "[scrubbed:unknown_key]"
    assert "9012345678" not in safe_text
