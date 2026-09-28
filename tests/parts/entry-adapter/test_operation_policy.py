from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

import entry_adapter


STG_COMPANY_ENV = "STG_DEFAULT_TENANT_ID"
STG_PROJECT_ENV = "STG_DEFAULT_UNUSED_PROJECT_ID"
STANDALONE_CALLER_PREFIX = "invoke-token:"


def _verified_scope() -> dict[str, str]:
    return {"tenant_id": "verified-tenant"}


def _operation_app(
    operation_name: str,
    *,
    middleware_state: dict[str, object] | None = None,
    verified_scope: dict[str, str] | None = None,
):
    app = FastAPI()
    if verified_scope is not None:
        app.state.verify_scope = lambda _request: verified_scope
    captured: dict[str, object] = {}

    if middleware_state is not None:

        @app.middleware("http")
        async def stamp_middleware_identity(request: Request, call_next):
            for key, value in middleware_state.items():
                setattr(request.state, key, value)
            return await call_next(request)

    @app.post("/entry")
    async def entry(request: Request):
        if hasattr(entry_adapter, "authenticate"):
            operation = getattr(entry_adapter.Operation, operation_name)
            context = entry_adapter.authenticate(request, operation=operation)
        else:
            # Failing-first probe against v0.3.0: it has no operation-aware API,
            # so all modes reach the same permissive authenticate_scope path.
            trust, scope = entry_adapter.authenticate_scope(request)
            context = SimpleNamespace(
                trust=trust,
                scope=scope,
                principal={"caller_id": getattr(request.state, "caller_id", "")},
                auth_source=None,
            )
        captured["context"] = context
        return {"status": "ok"}

    return app, captured


def _post(app: FastAPI, *, headers: dict[str, str] | None = None):
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post("/entry", headers=headers or {})


@pytest.mark.parametrize("operation_name", ["WRITE_INGEST", "WRITE_FEEDBACK", "RESUME"])
def test_standalone_token_is_forbidden_for_non_read_operations(
    monkeypatch, operation_name
):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _operation_app(operation_name, verified_scope=_verified_scope())

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_standalone_token_is_allowed_for_read_invoke(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _operation_app("READ_INVOKE", verified_scope=_verified_scope())

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    context = captured["context"]
    assert context.trust is TrustLevel.VERIFIED_EXTERNAL
    assert context.scope == _verified_scope()
    assert context.auth_source is entry_adapter.AuthSource.TOKEN


def test_middleware_verified_identity_is_allowed_for_write_ingest(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _operation_app(
        "WRITE_INGEST",
        middleware_state={
            "trust_level": TrustLevel.VERIFIED_EXTERNAL,
            "caller_id": "middleware-caller",
            **_verified_scope(),
        },
    )

    response = _post(app)

    assert response.status_code == 200
    context = captured["context"]
    assert context.auth_source is entry_adapter.AuthSource.MIDDLEWARE
    assert context.principal == {"caller_id": "middleware-caller"}


def test_stg_default_scope_is_forbidden_for_write_ingest(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _operation_app("WRITE_INGEST")

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_client_spoofed_reserved_caller_id_cannot_authorize_write(monkeypatch):
    """Even a header-copying middleware cannot mint the standalone principal."""
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _operation_app(
        "WRITE_INGEST",
        middleware_state={
            "trust_level": TrustLevel.VERIFIED_EXTERNAL,
            "caller_id": f"{STANDALONE_CALLER_PREFIX}attacker",
            **_verified_scope(),
        },
    )

    response = _post(app, headers={"x-caller-id": f"{STANDALONE_CALLER_PREFIX}attacker"})

    assert response.status_code == 403
    assert captured == {}


def test_partial_middleware_identity_is_not_overridden_by_valid_token(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _operation_app(
        "READ_INVOKE",
        middleware_state={"caller_id": "partial-middleware-caller"},
        verified_scope=_verified_scope(),
    )

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_write_requires_nonempty_middleware_principal(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _operation_app(
        "WRITE_INGEST",
        middleware_state={"trust_level": TrustLevel.VERIFIED_EXTERNAL, **_verified_scope()},
    )

    response = _post(app)

    assert response.status_code == 403
    assert captured == {}


def test_stg_default_auth_source_is_visible_for_read_invoke(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _operation_app("READ_INVOKE")

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    context = captured["context"]
    assert context.auth_source is entry_adapter.AuthSource.STG_DEFAULT
    assert context.scope == {"tenant_id": "stg-pc"}


def test_auth_context_is_frozen(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _operation_app("READ_INVOKE", verified_scope=_verified_scope())
    assert _post(app, headers={"authorization": "Bearer mock-entry-token"}).status_code == 200

    with pytest.raises(FrozenInstanceError):
        captured["context"].auth_source = entry_adapter.AuthSource.MIDDLEWARE


def test_operation_must_be_enum_not_bool():
    app = FastAPI()
    request = Request({"type": "http", "app": app, "headers": [], "state": {}})

    with pytest.raises(TypeError, match="Operation"):
        entry_adapter.authenticate(request, operation=True)


def test_package_exports_only_operation_aware_authentication_surface():
    assert entry_adapter.__all__ == [
        "authenticate",
        "AuthContext",
        "AuthSource",
        "Operation",
        "SCOPE_KEYS",
        "STG_DEFAULT_SCOPE_ENVS",
    ]
    assert not hasattr(entry_adapter, "authenticate_scope")
