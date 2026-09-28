from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

import entry_adapter
from entry_adapter import auth as entry_auth


def _scopeless_app(
    operation: entry_adapter.Operation,
    *,
    middleware_identity: bool = False,
):
    app = FastAPI()
    captured: dict[str, entry_adapter.AuthContext] = {}

    if middleware_identity:

        @app.middleware("http")
        async def stamp_middleware_identity(request: Request, call_next):
            request.state.trust_level = TrustLevel.VERIFIED_EXTERNAL
            request.state.caller_id = "middleware-caller"
            return await call_next(request)

    @app.post("/entry")
    async def entry(request: Request):
        captured["context"] = entry_adapter.authenticate(request, operation=operation)
        return {"status": "ok"}

    return app, captured


def _declare_no_scope(monkeypatch) -> None:
    monkeypatch.setattr(entry_auth, "SCOPE_KEYS", ())
    monkeypatch.setattr(entry_auth, "STG_DEFAULT_SCOPE_ENVS", {})


def _post(app: FastAPI, *, headers: dict[str, str] | None = None):
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post("/entry", headers=headers or {})


def test_scopeless_read_invoke_returns_empty_scope(monkeypatch):
    _declare_no_scope(monkeypatch)
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _scopeless_app(entry_adapter.Operation.READ_INVOKE)

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["context"].scope == {}
    assert captured["context"].auth_source is entry_adapter.AuthSource.TOKEN


def test_scopeless_standalone_token_cannot_write(monkeypatch):
    _declare_no_scope(monkeypatch)
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _scopeless_app(entry_adapter.Operation.WRITE_INGEST)

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_scopeless_middleware_identity_can_write(monkeypatch):
    _declare_no_scope(monkeypatch)
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _scopeless_app(
        entry_adapter.Operation.WRITE_INGEST,
        middleware_identity=True,
    )

    response = _post(app)

    assert response.status_code == 200
    assert captured["context"].scope == {}
    assert captured["context"].auth_source is entry_adapter.AuthSource.MIDDLEWARE


def test_scopeless_never_uses_stg_default(monkeypatch):
    _declare_no_scope(monkeypatch)
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_PRODUCTION_COMPANY_ID", "must-not-be-used")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "must-not-be-used")
    app, captured = _scopeless_app(entry_adapter.Operation.READ_INVOKE)

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["context"].scope == {}
    assert captured["context"].auth_source is entry_adapter.AuthSource.TOKEN


def test_declared_scope_still_uses_token_gated_stg_default(monkeypatch):
    monkeypatch.setattr(entry_auth, "SCOPE_KEYS", ("case_id",))
    monkeypatch.setattr(
        entry_auth,
        "STG_DEFAULT_SCOPE_ENVS",
        {"case_id": "STG_DEFAULT_CASE_ID"},
    )
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_CASE_ID", "stg-case")
    app, captured = _scopeless_app(entry_adapter.Operation.READ_INVOKE)

    response = _post(app, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["context"].scope == {"case_id": "stg-case"}
    assert captured["context"].auth_source is entry_adapter.AuthSource.STG_DEFAULT
