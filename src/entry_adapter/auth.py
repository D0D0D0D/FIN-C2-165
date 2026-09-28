# PART: entry-adapter v0.4.0 (parts@01ca509)
"""Authenticate entry callers under an explicit operation policy.

Client headers are never read for scope or principal identity. Scope comes
only from verified request state (or the injected ``app.state.verify_scope``
seam), with a token-gated STG default fallback for read-only entry calls.

Scope contract (v0.4.0): the template declares its KB partition key names in
``SCOPE_KEYS`` (domain vocabulary, e.g. ``("production_company_id",
"project_id")``), or explicitly declares ``()`` when the domain has no such
partition. ``authenticate`` returns a frozen ``AuthContext`` whose scope dict
is keyed by the declaration; every declared key must resolve to a clean
non-empty string or the request is rejected (fail closed).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, Request
from framework.schemas.trust_level import TrustLevel

from src.scope import SCOPE_KEYS as KB_SCOPE_KEYS


# Adaptation point: declare the template's KB partition keys in domain
# vocabulary. Each key doubles as the verified request.state attribute name
# the auth middleware stamps.
SCOPE_KEYS: tuple[str, ...] = KB_SCOPE_KEYS

# Adaptation point: STG default env var per scope key (used only after
# entry-token authentication).
STG_DEFAULT_SCOPE_ENVS: dict[str, str] = {"tenant_id": "STG_DEFAULT_TENANT_ID"}


class Operation(str, Enum):
    """Caller-declared operation whose policy is enforced by this module."""

    READ_INVOKE = "read_invoke"
    WRITE_INGEST = "write_ingest"
    WRITE_FEEDBACK = "write_feedback"
    RESUME = "resume"


class AuthSource(str, Enum):
    """Verified source of the resolved entry authentication context."""

    TOKEN = "token"
    MIDDLEWARE = "middleware"
    STG_DEFAULT = "stg_default"


@dataclass(frozen=True)
class AuthContext:
    """Verified trust, partition scope, principal, and authentication source."""

    trust: TrustLevel
    scope: dict[str, str]
    principal: dict[str, str]
    auth_source: AuthSource


_UNSTAMPED = object()
_STANDALONE_CALLER_PREFIX = "invoke-token:"
_STANDALONE_CALLER_ID = f"{_STANDALONE_CALLER_PREFIX}standalone"
_MIDDLEWARE_ONLY_OPERATIONS = frozenset(
    {Operation.WRITE_INGEST, Operation.WRITE_FEEDBACK, Operation.RESUME}
)


def authenticate(request: Request, *, operation: Operation) -> AuthContext:
    """Authenticate ``request`` and enforce the declared operation policy.

    ``operation`` must be an ``Operation`` enum member. Standalone token and
    STG-default contexts are admitted only for ``READ_INVOKE``; write operations
    and ``RESUME`` require a verified middleware identity with a non-empty,
    non-reserved ``caller_id``.
    """
    if not isinstance(operation, Operation):
        raise TypeError("entry_adapter: operation must be an Operation enum member")
    _validate_scope_config()
    trust, auth_source, caller_id = _resolve_identity(request)
    _require_verified_trust(trust)
    scope, used_stg_default = _require_scope(
        request, token_authenticated=auth_source is AuthSource.TOKEN
    )
    if used_stg_default:
        auth_source = AuthSource.STG_DEFAULT
    _require_operation_allowed(operation, auth_source)
    _require_operation_principal(operation, auth_source, caller_id)
    return AuthContext(
        trust=trust,
        scope=scope,
        principal={"caller_id": caller_id},
        auth_source=auth_source,
    )


def _validate_scope_config() -> None:
    """Fail loudly on a misconfigured SCOPE_KEYS declaration (deployment error)."""
    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("entry_adapter: SCOPE_KEYS must be a tuple of key names")
    for key in SCOPE_KEYS:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise RuntimeError(f"entry_adapter: SCOPE_KEYS entries must be identifier strings: {SCOPE_KEYS!r}")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError(f"entry_adapter: SCOPE_KEYS contains duplicates: {SCOPE_KEYS!r}")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError(
            "entry_adapter: STG_DEFAULT_SCOPE_ENVS must be a mapping whose keys exactly match SCOPE_KEYS"
        )
    for env_name in STG_DEFAULT_SCOPE_ENVS.values():
        if (
            not isinstance(env_name, str)
            or not env_name
            or env_name != env_name.strip()
            or "\n" in env_name
            or "\r" in env_name
        ):
            raise RuntimeError(
                f"entry_adapter: STG_DEFAULT_SCOPE_ENVS values must be clean env var names: "
                f"{STG_DEFAULT_SCOPE_ENVS!r}"
            )


def _coerce_trust(value) -> TrustLevel:
    """Normalize a request.state.trust_level to a TrustLevel enum.

    Middleware may stamp the attribute as the enum OR as its raw string value
    ("ANONYMOUS"). Normalize by value so both forms behave identically; anything
    unrecognized falls back to the secure default (ANONYMOUS).
    """
    if isinstance(value, TrustLevel):
        return value
    try:
        return TrustLevel(value)
    except (ValueError, KeyError, TypeError):
        return TrustLevel.ANONYMOUS


def _middleware_present(request: Request) -> bool:
    """Return whether any middleware-owned identity attribute was stamped.

    A partial stamp still counts. Otherwise a valid bearer could overwrite a
    broken/partial middleware identity and select the standalone path.
    """
    identity_attrs = ("trust_level", "caller_id", *SCOPE_KEYS)
    return any(getattr(request.state, attr, _UNSTAMPED) is not _UNSTAMPED for attr in identity_attrs)


def _clean_caller_id(value) -> str:
    if value is _UNSTAMPED:
        return ""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise HTTPException(status_code=403, detail="Verified middleware principal is required.")
    return value


def _resolve_identity(request: Request) -> tuple[TrustLevel, AuthSource | None, str]:
    """Resolve trust/source/principal without allowing bearer override of middleware."""
    if _middleware_present(request):
        trust = _coerce_trust(getattr(request.state, "trust_level", TrustLevel.ANONYMOUS))
        caller_id = _clean_caller_id(getattr(request.state, "caller_id", _UNSTAMPED))
        if caller_id.startswith(_STANDALONE_CALLER_PREFIX):
            raise HTTPException(status_code=403, detail="Reserved standalone caller_id is not allowed.")
        return trust, AuthSource.MIDDLEWARE, caller_id

    # S-3 entry-point exception: INVOKE_AUTH_TOKEN is read from os.environ only
    # here because it authenticates the caller before any InvocationContext exists.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        return TrustLevel.VERIFIED_EXTERNAL, AuthSource.TOKEN, _STANDALONE_CALLER_ID
    return TrustLevel.ANONYMOUS, None, ""


def _require_verified_trust(trust: TrustLevel) -> None:
    """Reject callers without externally verified or internal trust."""
    if trust not in {TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL}:
        raise HTTPException(status_code=403, detail="Verified caller trust is required.")


def _require_operation_allowed(operation: Operation, auth_source: AuthSource | None) -> None:
    """Keep standalone token and STG-default contexts read-only."""
    if auth_source is AuthSource.TOKEN and operation is not Operation.READ_INVOKE:
        raise HTTPException(status_code=403, detail="Verified middleware identity is required.")
    if auth_source is AuthSource.STG_DEFAULT and operation is not Operation.READ_INVOKE:
        raise HTTPException(status_code=403, detail="Verified middleware identity is required.")


def _require_operation_principal(
    operation: Operation, auth_source: AuthSource | None, caller_id: str
) -> None:
    """Require a concrete middleware principal for write and resume operations."""
    if (
        operation in _MIDDLEWARE_ONLY_OPERATIONS
        and auth_source is AuthSource.MIDDLEWARE
        and not caller_id
    ):
        raise HTTPException(status_code=403, detail="Verified middleware principal is required.")


def _valid_scope_value(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _normalize_scope(candidate) -> dict[str, str] | None:
    """Accept only a mapping carrying every declared key with a clean value."""
    if not isinstance(candidate, Mapping):
        return None
    scope = {key: candidate.get(key) for key in SCOPE_KEYS}
    if any(not _valid_scope_value(value) for value in scope.values()):
        return None
    return scope


def _scope_from_verified_state(request: Request) -> dict[str, object] | None:
    """Read verified scope attrs; None only when no attr was stamped at all.

    Explicitly stamped empty/None values are NOT "absent" — they mean a broken
    verifier and are returned as-is so the caller rejects them with 403 instead
    of silently falling back to the STG default.
    """
    values = {key: getattr(request.state, key, _UNSTAMPED) for key in SCOPE_KEYS}
    if all(value is _UNSTAMPED for value in values.values()):
        return None
    return {key: (None if value is _UNSTAMPED else value) for key, value in values.items()}


def _scope_from_stg_default() -> dict[str, str] | None:
    # No cleanup of env values: a padded/multiline STG env var is a deployment
    # typo that must surface as 403, not be silently repaired.
    return _normalize_scope({key: os.environ.get(STG_DEFAULT_SCOPE_ENVS[key], "") for key in SCOPE_KEYS})


def _require_scope(
    request: Request, *, token_authenticated: bool
) -> tuple[dict[str, str], bool]:
    """Return verified scope and whether the token-gated STG default supplied it.

    The STG fallback fires ONLY when no verified scope is present at all. A
    present-but-invalid scope is a 403 and is never rescued by the fallback.
    An explicit empty SCOPE_KEYS declaration means the domain has no partition;
    it returns an empty scope without consulting verified state or STG defaults.
    """
    if not SCOPE_KEYS:
        return {}, False
    verify_scope = getattr(request.app.state, "verify_scope", _scope_from_verified_state)
    candidate = verify_scope(request)
    if candidate is None:
        scope = _scope_from_stg_default() if token_authenticated else None
        if scope is None:
            raise HTTPException(status_code=403, detail="Authenticated caller scope is required.")
        return scope, True
    if not isinstance(candidate, Mapping):
        raise RuntimeError(
            "entry_adapter: verify_scope must return a Mapping keyed by SCOPE_KEYS or None (v0.3.0 contract)"
        )
    scope = _normalize_scope(candidate)
    if scope is None:
        raise HTTPException(status_code=403, detail="Authenticated caller scope is required.")
    return scope, False
