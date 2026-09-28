from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

import entry_adapter
from entry_adapter import auth as entry_auth


STG_COMPANY_ENV = "STG_DEFAULT_TENANT_ID"
STG_PROJECT_ENV = "STG_DEFAULT_UNUSED_PROJECT_ID"


def _invoke_app(*, preset_trust: TrustLevel | None = None, verify_scope=None):
    app = FastAPI()
    app.state.preset_trust = preset_trust
    if verify_scope is not None:
        app.state.verify_scope = verify_scope
    captured: dict[str, object] = {}

    @app.middleware("http")
    async def set_test_trust(request: Request, call_next):
        if app.state.preset_trust is not None:
            request.state.trust_level = app.state.preset_trust
        return await call_next(request)

    @app.post("/invoke")
    async def invoke(request: Request):
        context = entry_adapter.authenticate(request, operation=entry_adapter.Operation.READ_INVOKE)
        captured["trust"] = context.trust
        captured["scope"] = context.scope
        return {"status": "ok"}

    return app, captured


def _verified_scope(**overrides):
    scope = {"tenant_id": "verified-tenant"}
    scope.update(overrides)
    return scope


def test_package_exports_only_composed_authentication_surface():
    assert entry_adapter.__all__ == [
        "authenticate",
        "AuthContext",
        "AuthSource",
        "Operation",
        "SCOPE_KEYS",
        "STG_DEFAULT_SCOPE_ENVS",
    ]
    assert not hasattr(entry_adapter, "authenticate_scope")
    assert not hasattr(entry_adapter, "resolve_trust")
    assert not hasattr(entry_adapter, "require_verified_trust")
    assert not hasattr(entry_adapter, "require_scope")


def test_stg_default_env_names_are_derived_from_scope_keys():
    assert entry_adapter.SCOPE_KEYS == ("tenant_id",)
    assert entry_adapter.STG_DEFAULT_SCOPE_ENVS == {"tenant_id": STG_COMPANY_ENV}


@pytest.mark.parametrize("entry_token", [None, "mock-entry-token"])
def test_client_headers_cannot_supply_or_override_scope(monkeypatch, entry_token):
    monkeypatch.delenv(STG_COMPANY_ENV, raising=False)
    monkeypatch.delenv(STG_PROJECT_ENV, raising=False)
    if entry_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        headers = {"x-production-company-id": "tenant-other", "x-project-id": "org-other"}
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", entry_token)
        headers = {
            "authorization": f"Bearer {entry_token}",
            "x-production-company-id": "tenant-other",
            "x-project-id": "org-other",
        }
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers=headers)

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize(
    ("expected_token", "authorization", "expected_status"),
    [
        (None, None, 403),
        ("mock-entry-token", None, 401),
        ("mock-entry-token", "Bearer wrong-token", 401),
    ],
)
def test_stg_default_scope_does_not_rescue_anonymous_caller(
    monkeypatch, expected_token, authorization, expected_status
):
    if expected_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", expected_token)
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app()
    headers = {} if authorization is None else {"authorization": authorization}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers=headers)

    assert response.status_code == expected_status
    assert captured == {}


def test_non_ascii_authorization_header_is_401_not_500(monkeypatch):
    """A non-ASCII Authorization header must fail closed as 401, never 500.

    Starlette decodes raw request-header bytes as latin-1, so an attacker-sent
    byte like 0xff reaches auth as a non-ASCII str ("Bearer \\xff..."). Passing
    such a str straight to secrets.compare_digest raises
    TypeError("comparing strings with non-ASCII characters is not supported")
    -> HTTP 500, letting the caller trigger errors at will. The byte comparison
    in auth._resolve_trust (supplied.encode()) keeps this a clean 401.
    Regression guard for a defect first found in a template entry point.
    """
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app()

    # Raw bytes header (not str): httpx/Starlette would otherwise ascii-encode a
    # str and never let 0xff onto the wire. This reproduces the latin-1 decode.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers=[(b"authorization", b"Bearer \xff\xfe")])

    assert response.status_code == 401
    assert captured == {}


def test_token_authenticated_request_uses_stg_default_scope(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["trust"] is TrustLevel.VERIFIED_EXTERNAL
    assert captured["scope"] == {"tenant_id": "stg-pc"}


def test_verify_scope_seam_takes_precedence_over_stg_default(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app(verify_scope=lambda _request: _verified_scope())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["scope"] == _verified_scope()


def test_legacy_tuple_verify_scope_seam_fails_loudly_not_403(monkeypatch):
    """A stale pre-v0.3.0 seam returning a tuple is a wiring bug (500), not 403."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _invoke_app(verify_scope=lambda _request: ("verified-pc", "verified-pj"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 500
    assert captured == {}


@pytest.mark.parametrize(
    "bad_scope",
    [
        _verified_scope(tenant_id=""),
        _verified_scope(tenant_id=" "),
        _verified_scope(tenant_id="tenant "),
        _verified_scope(tenant_id="tenant\n1"),
        _verified_scope(tenant_id=123),
        {"undeclared": "verified"},  # declared key missing
        {},
    ],
)
def test_invalid_seam_scope_is_403_and_never_rescued_by_stg_default(monkeypatch, bad_scope):
    """A present-but-invalid verified scope must 403 even when the STG
    fallback is fully armed (valid token + STG envs set). Only a completely
    absent scope (seam returns None) may fall back."""
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _invoke_app(verify_scope=lambda _request: bad_scope)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize("tenant_env", [" stg-tenant ", "stg-tenant\n", " "])
def test_padded_or_multiline_stg_env_values_are_403_not_silently_repaired(
    monkeypatch, tenant_env
):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, tenant_env)
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize("stamped_value", ["", None])
def test_explicitly_stamped_empty_scope_is_403_not_stg_rescue(monkeypatch, stamped_value):
    """A verifier that stamps the attrs with ''/None is broken, not absent —
    403 even with the STG fallback fully armed. Only a request whose state
    carries none of the declared attrs may fall back."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app()

    @app.middleware("http")
    async def stamp_empty_scope(request: Request, call_next):
        request.state.tenant_id = stamped_value
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_nonstring_stamped_verified_state_is_403_not_stg_rescue(monkeypatch):
    """A stamped but invalid one-axis value is a broken verifier, not absence."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app()

    @app.middleware("http")
    async def stamp_partial_scope(request: Request, call_next):
        request.state.tenant_id = 123
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403
    assert captured == {}


def test_verified_request_state_supplies_scope_via_declared_attr_names(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _invoke_app(preset_trust=TrustLevel.VERIFIED_EXTERNAL)

    @app.middleware("http")
    async def stamp_verified_scope(request: Request, call_next):
        request.state.tenant_id = "state-tenant"
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 200
    assert captured["scope"] == {"tenant_id": "state-tenant"}


def test_internal_request_state_passes_without_token(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _invoke_app(
        preset_trust=TrustLevel.INTERNAL,
        verify_scope=lambda _request: _verified_scope(tenant_id="internal-tenant"),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 200
    assert captured["trust"] is TrustLevel.INTERNAL


@pytest.mark.parametrize("preset_trust", [TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL])
def test_stg_default_never_fires_for_preset_trust_without_token_auth(monkeypatch, preset_trust):
    """STG default scope is reachable ONLY via entry-token authentication.

    A caller whose trust was set upstream (middleware) but whose verified scope
    is missing must get 403 even when STG env vars are present — otherwise a
    production deployment with leftover STG env silently serves the placeholder
    partition scope (cross-partition pollution). Kills the mutation that drops
    the token gate from the STG fallback.
    """
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv(STG_COMPANY_ENV, "stg-pc")
    monkeypatch.setenv(STG_PROJECT_ENV, "stg-pj")
    app, captured = _invoke_app(preset_trust=preset_trust)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 403
    assert captured == {}


def test_anonymous_request_without_configured_token_is_forbidden(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _invoke_app(verify_scope=lambda _request: _verified_scope())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize("preset_trust", [TrustLevel.ANONYMOUS, "ANONYMOUS"])
def test_anonymous_middleware_identity_is_not_overridden_by_token(monkeypatch, preset_trust):
    """Any middleware identity stamp wins, including enum/string ANONYMOUS.

    This is the v0.4.0 fail-closed replacement for token promotion after a
    middleware stamp: a bearer must not repair or override partial middleware
    identity state.
    """
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _invoke_app(
        preset_trust=preset_trust,
        verify_scope=lambda _request: _verified_scope(tenant_id="tenant"),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})
    assert response.status_code == 403
    assert captured == {}


def test_single_scope_key_configuration_works(monkeypatch):
    """A one-axis domain declares one key — no dummy second axis required."""
    monkeypatch.setattr(entry_auth, "SCOPE_KEYS", ("case_id",))
    monkeypatch.setattr(entry_auth, "STG_DEFAULT_SCOPE_ENVS", {"case_id": "STG_DEFAULT_CASE_ID"})
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_CASE_ID", "stg-case")
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["scope"] == {"case_id": "stg-case"}


@pytest.mark.parametrize(
    ("bad_keys", "bad_envs"),
    [
        ("case_id", {}),
        (None, {}),
        (42, {}),
        ({"case_id": "x"}, {"case_id": "E"}),
        (["case_id"], {"case_id": "E"}),  # list, not tuple
        (("case id",), {"case id": "E"}),
        (("case_id", "case_id"), {"case_id": "E"}),
        (("case_id",), {}),  # STG env mapping missing for a declared key
        (("case_id",), {"case_id": "E", "other": "F"}),  # extra env entry
        (("case_id",), {"case_id": ""}),  # empty env var name
        (("case_id",), {"case_id": 7}),  # non-string env var name
        (("case_id",), {"case_id": " STG_DEFAULT_CASE_ID "}),  # padded env var name
        (("case_id",), {"case_id": "STG\nDEFAULT"}),  # multiline env var name
        (("case_id",), ("STG_DEFAULT_CASE_ID",)),  # envs not a mapping
    ],
)
def test_misconfigured_scope_keys_fail_loudly(monkeypatch, bad_keys, bad_envs):
    monkeypatch.setattr(entry_auth, "SCOPE_KEYS", bad_keys)
    monkeypatch.setattr(entry_auth, "STG_DEFAULT_SCOPE_ENVS", bad_envs)
    app, captured = _invoke_app(preset_trust=TrustLevel.VERIFIED_EXTERNAL)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 500
    assert captured == {}
