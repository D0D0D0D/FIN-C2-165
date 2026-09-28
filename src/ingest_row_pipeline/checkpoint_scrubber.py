# PART: ingest-row-pipeline v0.3.1 (parts@01ca509)
"""Allowlist checkpoint scrubber for row ingest envelopes.

The scrubber runs before a graph invocation can checkpoint `user_input`.
Only explicitly safe control fields remain in the returned JSON. Free-form
scalar values are moved into a transient payload store and replaced with markers.
Client-supplied `payload_ref` is always discarded and replaced with a server
issued reference.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class PayloadStore(Protocol):
    def put(self, value: str, *, scope: Mapping[str, str], session_id: str) -> str: ...


@dataclass(frozen=True)
class CheckpointScrubPolicy:
    safe_keys: frozenset[str] = frozenset(
        {
            "mode",
            "record_id",
            "source_record_id",
            "observed_at",
            "occurred_at",
            "category",
            "record_type",
            "status",
            "op",
        }
    )
    # Template-declared KB partition keys (domain vocabulary, matching the
    # template's scope_keys declaration in scoped-retrieval / payload-store /
    # entry-adapter). Client-sent values under these names are NEVER kept:
    # they are scrubbed like any other input, and the verified values passed
    # via scrub_checkpoint_envelope(trusted_scope=...) are injected at the
    # top level of the returned envelope after scrubbing.
    scope_keys: frozenset[str] = frozenset()
    enum_value_keys: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            "mode": frozenset({"ingest", "invoke", "feedback"}),
            "op": frozenset({"create", "update", "delete", "redact", "record"}),
            "status": frozenset({"open", "in_progress", "closed", "cancelled"}),
            "record_type": frozenset({"row", "finding", "incident", "correction"}),
        }
    )
    safe_scalar_list_keys: frozenset[str] = frozenset({"source_ids"})
    safe_subtree_keys: frozenset[str] = frozenset()
    payload_ref_key: str = "payload_ref"
    max_safe_string_chars: int = 64


def scrub_checkpoint_envelope(
    raw_input: str | bytes,
    payload_store: PayloadStore,
    *,
    session_id: str,
    trusted_scope: Mapping[str, str] | None = None,
    policy: CheckpointScrubPolicy | None = None,
) -> str:
    """Return checkpoint-safe JSON and always issue a new server payload_ref.

    Scalar leaves outside the policy allowlists are stashed with their native
    JSON values and replaced by ``[scrubbed:path]`` string markers. Scalars
    explicitly admitted by ``safe_keys`` or ``safe_scalar_list_keys`` retain
    their native types; safe strings still pass the configured shape guard.
    ``safe_subtree_keys`` remain unchanged, and ``enum_value_keys`` retain only
    exact allowed strings.

    The stashed raw payload is bound to the verified scope + session via
    ``payload_store.put(value, scope=..., session_id=...)`` — matching
    payload-store v0.3.0 — so the vaulted body is never scope/session-free.

    When ``policy.scope_keys`` is declared, ``trusted_scope`` (the verified
    per-request scope dict, e.g. from entry-adapter's ``authenticate_scope``)
    is MANDATORY and is injected at the top level of the returned envelope
    after scrubbing — client-sent values under those names never survive.
    A missing/invalid ``trusted_scope`` or ``session_id`` raises
    ``ValueError`` (wiring bug).
    """

    policy = policy or CheckpointScrubPolicy()
    scope = _require_trusted_scope(trusted_scope, policy)
    if not _valid_scope_value(session_id):
        raise ValueError("scrub_checkpoint_envelope: session_id must be a clean non-empty string")
    raw_text = raw_input.decode("utf-8", errors="replace") if isinstance(raw_input, bytes) else str(raw_input)
    extra: dict[str, Any] = {}

    try:
        envelope = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        ref = payload_store.put(
            json.dumps({"__raw_input__": raw_text}, ensure_ascii=False, default=str),
            scope=scope,
            session_id=session_id,
        )
        return json.dumps(
            {**scope, policy.payload_ref_key: ref, "__unparsable__": True}, ensure_ascii=False
        )

    if not isinstance(envelope, dict):
        ref = payload_store.put(
            json.dumps({"__raw_input__": envelope}, ensure_ascii=False, default=str),
            scope=scope,
            session_id=session_id,
        )
        return json.dumps(
            {**scope, policy.payload_ref_key: ref, "__unparsable__": True}, ensure_ascii=False
        )

    envelope = dict(envelope)
    envelope.pop(policy.payload_ref_key, None)

    payload = envelope.get("payload")
    bundle: dict[str, Any] = {}
    if isinstance(payload, dict):
        if "records" in payload:
            bundle["records"] = payload["records"]
        remainder = {k: v for k, v in payload.items() if k != "records"}
        scrubbed_payload = _scrub_node(remainder, extra, "payload", policy)
    else:
        scrubbed_payload = _scrub_node(payload, extra, "payload", policy) if payload is not None else None

    env_rest = {k: v for k, v in envelope.items() if k != "payload"}
    scrubbed_env = _scrub_node(env_rest, extra, "", policy)
    if isinstance(scrubbed_env, dict) and scrubbed_payload is not None:
        scrubbed_env["payload"] = scrubbed_payload
    elif not isinstance(scrubbed_env, dict):
        scrubbed_env = {}

    if extra:
        bundle["__extra__"] = extra

    # Always reissue a server-side ref. Even an all-control envelope receives
    # a new ref so a forged client ref can never select stored raw content.
    ref = payload_store.put(
        json.dumps(bundle, ensure_ascii=False, default=str), scope=scope, session_id=session_id
    )
    # Inject the verified scope AFTER scrubbing: this overwrites whatever the
    # client sent (or the scrub marker it became) with the trusted values.
    scrubbed_env.update(scope)
    scrubbed_env[policy.payload_ref_key] = ref
    return json.dumps(scrubbed_env, ensure_ascii=False, default=str)


_RESERVED_ENVELOPE_KEYS = frozenset(
    {"mode", "op", "status", "payload", "payload_ref", "__unparsable__", "__extra__", "__raw_input__"}
)


def _require_trusted_scope(
    trusted_scope: Mapping[str, str] | None, policy: CheckpointScrubPolicy
) -> dict[str, str]:
    """Validate the verified scope against the declared policy.scope_keys.

    Raises ValueError on any deviation — a wrong trusted_scope is a server
    wiring bug, and silently proceeding would checkpoint an unscoped or
    mis-scoped envelope.
    """
    declared = policy.scope_keys
    if not declared:
        if trusted_scope:
            raise ValueError(
                "scrub_checkpoint_envelope: trusted_scope supplied but policy.scope_keys is empty — "
                "declare the keys in CheckpointScrubPolicy(scope_keys=...)"
            )
        return {}
    for key in declared:
        if not isinstance(key, str) or not key.isidentifier() or key in _RESERVED_ENVELOPE_KEYS or key == policy.payload_ref_key:
            raise ValueError(f"scrub_checkpoint_envelope: invalid scope key declaration: {key!r}")
    if not isinstance(trusted_scope, Mapping) or set(trusted_scope) != set(declared):
        raise ValueError(
            "scrub_checkpoint_envelope: trusted_scope must supply exactly the declared scope_keys "
            f"{sorted(declared)}"
        )
    for key in declared:
        if not _valid_scope_value(trusted_scope[key]):
            raise ValueError(
                f"scrub_checkpoint_envelope: trusted_scope[{key!r}] must be a clean non-empty string"
            )
    return {key: trusted_scope[key] for key in sorted(declared)}


def _valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _scrub_node(node: Any, extra: dict[str, Any], path: str, policy: CheckpointScrubPolicy) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if key in policy.enum_value_keys:
                if isinstance(value, str) and value in policy.enum_value_keys[key]:
                    out[key] = value
                else:
                    out[key] = _scrub_node(value, extra, child_path, policy)
                continue
            if key in policy.safe_subtree_keys:
                out[key] = value
            elif key in policy.safe_scalar_list_keys and isinstance(value, list):
                out[key] = [
                    _keep_or_scrub_safe_scalar(item, extra, f"{child_path}[{idx}]", policy)
                    for idx, item in enumerate(value)
                    if not isinstance(item, (dict, list))
                ]
            elif key in policy.safe_keys:
                out[key] = (
                    _scrub_node(value, extra, child_path, policy)
                    if isinstance(value, (dict, list))
                    else _keep_or_scrub_safe_scalar(value, extra, child_path, policy)
                )
            else:
                out[key] = _scrub_node(value, extra, child_path, policy)
        return out
    if isinstance(node, list):
        return [_scrub_node(item, extra, f"{path}[{idx}]", policy) for idx, item in enumerate(node)]
    extra[path or "__value__"] = node
    return f"[scrubbed:{path or '__value__'}]"


def _keep_or_scrub_safe_scalar(value: Any, extra: dict[str, Any], path: str, policy: CheckpointScrubPolicy) -> Any:
    if isinstance(value, str) and not _safe_string_shape(value, policy):
        extra[path or "__value__"] = value
        return f"[scrubbed:{path or '__value__'}]"
    return value


def _safe_string_shape(value: str, policy: CheckpointScrubPolicy) -> bool:
    if "\n" in value or "\r" in value:
        return False
    return len(value) <= policy.max_safe_string_chars
