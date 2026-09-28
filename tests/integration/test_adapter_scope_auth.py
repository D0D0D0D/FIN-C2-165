"""Integration — entry-adapter authentication and tenant-scope resolution.

Client headers can never supply or override the authenticated tenant scope; the
token-gated STG default only rescues a token-authenticated caller, never an
anonymous one (entry-adapter contract).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.entry_adapter import AuthContext, AuthSource, Operation
from src.services import wire_services

app = server.app


def _stub_agent(monkeypatch, invoke):
    class StubAgent:
        _secrets_provider = server._secrets_provider

        def invoke(self, envelope, *, ctx, input_context):
            return invoke(envelope, ctx=ctx, input_context=input_context)

    monkeypatch.setattr(server, "build_agent", lambda **_kwargs: StubAgent())


def _middleware_auth(monkeypatch, *, tenant: str = "tenant-verified", operations=None):
    seen = operations if operations is not None else []

    def fake_authenticate(_request, *, operation):
        seen.append(operation)
        return AuthContext(
            trust=TrustLevel.VERIFIED_EXTERNAL,
            scope={"tenant_id": tenant},
            principal={"caller_id": "middleware-caller"},
            auth_source=AuthSource.MIDDLEWARE,
        )

    monkeypatch.setattr(server, "authenticate", fake_authenticate)
    return seen


@pytest.mark.parametrize("entry_token", [None, "mock-entry-token"])
def test_client_headers_cannot_supply_scope(monkeypatch, entry_token):
    monkeypatch.delenv("STG_DEFAULT_TENANT_ID", raising=False)
    if entry_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        headers = {"x-tenant-id": "tenant-other"}
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", entry_token)
        headers = {"authorization": f"Bearer {entry_token}", "x-tenant-id": "tenant-other"}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/ingest", headers=headers, json={"payload": []})

    assert response.status_code == 403


def test_verified_scope_ignores_client_headers(monkeypatch):
    monkeypatch.delenv("STG_DEFAULT_TENANT_ID", raising=False)
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    captured: dict[str, object] = {}

    def fake_invoke(_envelope, *, ctx, input_context):
        captured["scope"] = input_context["configurable"]
        return {"status": "ok"}

    _middleware_auth(monkeypatch)
    _stub_agent(monkeypatch, fake_invoke)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/ingest",
            headers={"authorization": "Bearer mock-entry-token", "x-tenant-id": "tenant-other"},
            json={"payload": []},
        )

    assert response.status_code == 200
    assert captured["scope"]["tenant_id"] == "tenant-verified"


def test_token_authenticated_stg_default_cannot_authorize_preflight_write(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
    invoked = False

    def fake_invoke(_envelope, *, ctx, input_context):
        nonlocal invoked
        invoked = True
        return {"status": "ok"}

    _stub_agent(monkeypatch, fake_invoke)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/preflight", headers={"authorization": "Bearer mock-entry-token"}, json={"payload": {}})

    assert response.status_code == 403
    assert invoked is False


def test_preflight_standalone_token_cannot_register_a_case(monkeypatch):
    """Regression: /preflight writes a shipment_packets case header, so the
    read-only standalone/STG credential must be rejected before graph entry."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
    runtime_services = wire_services()
    monkeypatch.setattr(server, "services", runtime_services)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/preflight",
            headers={"authorization": "Bearer mock-entry-token"},
            json={
                "payload": {
                    "lc": {"required_docs": ["invoice"], "amount": 100, "tolerance_pct": 0},
                    "documents": {"invoice": {"amount": 100}},
                },
                "session_id": "preflight-write-auth-regression",
            },
        )

    assert response.status_code == 403
    assert runtime_services["kb_backend"].cases == {}


def test_preflight_case_ref_is_server_minted_ignoring_client(monkeypatch):
    """A client-supplied preflight case_ref is ignored; each
    new preflight is server-minted, so a caller cannot attach a new lifecycle to
    an existing same-tenant case_ref by re-sending it (design §8-1)."""
    import json

    _middleware_auth(monkeypatch)
    seen: list[str] = []

    def fake_invoke(envelope, *, ctx, input_context):
        seen.append(json.loads(envelope)["case_ref"])
        return {"status": "ok"}

    _stub_agent(monkeypatch, fake_invoke)

    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(2):
            resp = client.post(
                "/preflight",
                headers={"authorization": "Bearer mock-entry-token"},
                json={"payload": {}, "case_ref": "attacker-reused-case"},
            )
            assert resp.status_code == 200

    # neither invocation used the client's case_ref, and the two are distinct
    assert "attacker-reused-case" not in seen
    assert len(seen) == 2 and seen[0] != seen[1]


def test_invoke_endpoint_dispatches_on_mode(monkeypatch):
    """The single-endpoint /invoke contract parses the input envelope, dispatches
    on mode, and routes through the same _run path (STG evidence / AgentGateway)."""
    import json

    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
    seen: list[dict] = []

    def fake_invoke(envelope, *, ctx, input_context):
        seen.append(json.loads(envelope))
        return {"status": "success", "formatted_output": {}}

    _middleware_auth(monkeypatch)
    _stub_agent(monkeypatch, fake_invoke)

    envelope = json.dumps({"mode": "ingest", "payload": [{"record_id": "r1", "record_type": "review_point", "review_point": "x"}]})
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/invoke",
            headers={"x-tenant-id": "attacker"},
            json={"input": envelope, "session_id": "s1"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert seen and seen[0]["mode"] == "ingest"
    # tenant came from verified middleware auth, never the client body/header
    assert seen[0]["scope"]["tenant_id"] == "tenant-verified"


def test_http_all_modes_declare_the_actual_operation(monkeypatch):
    operations: list[Operation] = []
    _middleware_auth(monkeypatch, operations=operations)
    _stub_agent(monkeypatch, lambda *_args, **_kwargs: {"status": "success"})

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/preflight", json={"payload": {}}).status_code == 200
        assert client.post("/ingest", json={"payload": []}).status_code == 200
        assert client.post("/feedback", json={"payload": {}, "case_ref": "case"}).status_code == 200

    assert operations == [
        Operation.WRITE_INGEST,
        Operation.WRITE_INGEST,
        Operation.WRITE_FEEDBACK,
    ]


def test_real_http_graph_runs_all_three_modes_in_sequence(monkeypatch):
    runtime_services = wire_services()
    monkeypatch.setattr(server, "services", runtime_services)
    monkeypatch.setattr(server, "_authorized_approvers", ["middleware-caller"])
    operations: list[Operation] = []
    _middleware_auth(monkeypatch, operations=operations)

    ingest_payload = [
        {
            "record_id": "http-rp-1",
            "observed_at": "2026-01-01",
            "review_point": "invoice amount mismatch",
        }
    ]
    preflight_payload = {
        "lc": {"required_docs": ["invoice"], "amount": 100, "tolerance_pct": 0},
        "documents": {"invoice": {"amount": 100}},
    }
    runtime_services["kb_backend"].register_case(
        tenant_id="tenant-verified", case_ref="case-http"
    )
    feedback_payload = {
        "case_ref": "case-http",
        "event_type": "point_confirmed",
        "event_id": "event-http",
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        ingest = client.post("/ingest", json={"payload": ingest_payload, "session_id": "http-i"})
        preflight = client.post(
            "/preflight", json={"payload": preflight_payload, "session_id": "http-p"}
        )
        feedback = client.post(
            "/feedback",
            json={"payload": feedback_payload, "case_ref": "case-http", "session_id": "http-f"},
        )

    assert ingest.status_code == preflight.status_code == feedback.status_code == 200
    assert ingest.json()["status"] == "success"
    assert preflight.json()["status"] == "success"
    assert feedback.json()["status"] == "success"
    assert operations == [
        Operation.WRITE_INGEST,
        Operation.WRITE_INGEST,
        Operation.WRITE_FEEDBACK,
    ]


@pytest.mark.parametrize("path", ["/preflight", "/ingest", "/feedback"])
def test_http_write_modes_reject_standalone_token_and_stg_default(monkeypatch, path):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
    _stub_agent(monkeypatch, lambda *_args, **_kwargs: {"status": "success"})

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            path,
            headers={"authorization": "Bearer mock-entry-token"},
            json={"payload": {}, "case_ref": "case"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "bad_input",
    [
        "not-json",
        '{"mode": "bogus", "payload": {}}',
        '["not", "an", "object"]',
        '{"mode": [], "payload": []}',       # unhashable mode must not return 500
        '{"mode": "ingest"}',                # missing payload must fail closed, not reach _run
        '{"mode": "ingest", "payload": null}',
    ],
)
def test_invoke_rejects_bad_envelope(monkeypatch, bad_input):
    """A malformed envelope, unknown/unhashable mode, or missing payload fails
    closed with 400 (never reaches the graph)."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-entry-token"},
            json={"input": bad_input, "session_id": "s1"},
        )

    assert resp.status_code == 400


def test_invoke_preflight_case_ref_still_server_minted(monkeypatch):
    """The server-minted case_ref rule also holds through /invoke: a client value is
    ignored for preflight, which always server-mints its case_ref."""
    import json

    _middleware_auth(monkeypatch)
    seen: list[str] = []

    def fake_invoke(envelope, *, ctx, input_context):
        seen.append(json.loads(envelope)["case_ref"])
        return {"status": "success", "formatted_output": {}}

    _stub_agent(monkeypatch, fake_invoke)

    env = json.dumps({"mode": "preflight", "case_ref": "attacker-reused-case", "payload": {}})
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-entry-token"},
            json={"input": env, "session_id": "s1"},
        )

    assert resp.status_code == 200
    assert seen and seen[0] != "attacker-reused-case"


@pytest.mark.parametrize(("entry_token", "expected"), [(None, 403), ("mock-entry-token", 401)])
def test_stg_default_does_not_rescue_anonymous(monkeypatch, entry_token, expected):
    if entry_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", entry_token)
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/ingest", json={"payload": []})

    assert response.status_code == expected
