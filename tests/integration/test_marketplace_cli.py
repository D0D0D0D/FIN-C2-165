"""Marketplace adapter contract tests for FIN-C2-165."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config

import cli
from src.graph.graph import TradeFinanceLCPreflightAgent
from src.payload_store import PayloadStoreError, mint_reference
from src.services import InMemoryKBBackend, KBBackendError, wire_services

TENANT = "marketplace-test-tenant"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

PREFLIGHT_PAYLOAD = {
    "lc": {
        "term_id": "sample-term",
        "required_docs": ["commercial_invoice", "insurance_certificate"],
        "amount": 100_000,
        "tolerance_pct": 5,
        "presentation_period_days": 21,
        "special_conditions": "The invoice must state the declared origin.",
    },
    "documents": {
        "commercial_invoice": {
            "link_id": "sample-invoice",
            "amount": 106_000,
            "text": "Fictitious commercial invoice.",
        },
        "bill_of_lading": {
            "link_id": "sample-transport",
            "on_board_date": "2026-08-01",
        },
    },
    "as_of_date": "2026-08-12",
}


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.calls += 1
        assert isinstance(messages, list)
        return {
            "content": json.dumps(
                {
                    "inconsistency_candidates": [],
                    "bank_inquiry_points": [],
                    "correction_suggestions": [],
                }
            ),
            "model": "fake-marketplace-llm",
            "usage": {},
        }

    def stream(self, messages: list[Any]):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> "FakeLLM":
        del tools
        return self


@pytest.fixture(autouse=True)
def deployment_environment(monkeypatch):
    values = {
        "STG_DEFAULT_TENANT_ID": TENANT,
        "AZURE_OPENAI_API_KEY": "test-api-key",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _file_config() -> dict[str, Any]:
    return load_config(str(CONFIG_PATH))


def _marketplace_agent(fake_llm: FakeLLM, *, provision_first: bool) -> cli.MarketplaceGraph:
    config = _file_config()
    config["services"] = {"llm": fake_llm}
    agent = cli.MarketplaceGraph(config=config)
    provider = cli._AllowlistedEnvironmentProvider()
    if provision_first:
        agent.provision_secrets(provider)
        agent.compile()
    else:
        agent.compile()
        agent.provision_secrets(provider)
    return agent


def _invoke_marketplace(agent: cli.MarketplaceGraph, user_input: str) -> dict[str, Any]:
    ctx = InvocationContext(
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(
            user_input,
            ctx=ctx,
            input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
        )


@pytest.mark.parametrize("provision_first", [False, True], ids=["sdk-1.0.1", "sdk-1.0.2"])
def test_runner_orders_execute_real_preflight_without_conversation_leak(provision_first):
    fake_llm = FakeLLM()
    agent = _marketplace_agent(fake_llm, provision_first=provision_first)

    result = _invoke_marketplace(agent, json.dumps(PREFLIGHT_PAYLOAD))
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "success"
    assert result["output"]
    assert fake_llm.calls >= 1
    assert "CANARY" not in serialized
    assert "llm_not_configured" not in serialized
    assert "SAMPLE-LC-001" in serialized


def _invoke_unwrapped_backend(backend: InMemoryKBBackend) -> dict[str, Any]:
    fake_llm = FakeLLM()
    services = wire_services()
    services["kb_backend"] = backend
    services["llm"] = fake_llm
    agent = TradeFinanceLCPreflightAgent(
        config={
            **_file_config(),
            "services": services,
            "trusted_scope": {"tenant_id": TENANT},
            "session_id": "compile-session",
            "authorized_approvers": [],
        }
    )
    agent.compile()
    session_id = "raw-backend-session"
    scope = {"tenant_id": TENANT}
    ref = services["payload_store"].put(PREFLIGHT_PAYLOAD, scope=scope, session_id=session_id)
    envelope = json.dumps(
        {
            "mode": "preflight",
            "case_ref": mint_reference(),
            "scope": scope,
            "plan_draft_ref": ref,
        }
    )
    try:
        return agent.invoke(
            envelope,
            ctx=InvocationContext(
                session_id=session_id,
                caller_id="verified-caller",
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            ),
            input_context={"configurable": scope},
        )
    finally:
        services["payload_store"].delete(ref, scope=scope, session_id=session_id)


def test_register_case_writes_unwrapped_backend_but_marketplace_noops():
    writable = InMemoryKBBackend()
    before = len(writable.cases)
    writable_result = _invoke_unwrapped_backend(writable)

    marketplace = _marketplace_agent(FakeLLM(), provision_first=False)
    readonly = marketplace.config["services"]["kb_backend"]
    readonly_result = _invoke_marketplace(marketplace, json.dumps(PREFLIGHT_PAYLOAD))

    assert writable_result["status"] == "success"
    assert len(writable.cases) == before + 1
    assert readonly_result["status"] == "success"
    assert readonly.cases == {}


def test_sample_seed_is_idempotent_and_tenant_scoped():
    backend = cli._ReadOnlyKBBackend()
    cli._seed_sample_kb(backend, TENANT)
    cli._seed_sample_kb(backend, TENANT)

    assert len(backend.records) == 12
    assert {record["metadata"]["tenant_id"] for record in backend.records} == {TENANT}
    assert all(record["source_id"].startswith("SAMPLE-LC-") for record in backend.records)


def test_sample_loader_fails_loud_when_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_SAMPLE_KB_PATH", tmp_path / "missing.sample.json")

    with pytest.raises(FileNotFoundError):
        cli._validate_sample_data()


@pytest.mark.parametrize(
    ("content", "error_type", "message"),
    [
        ("{not-json", json.JSONDecodeError, None),
        (json.dumps({"source_id": "SAMPLE-LC-001"}), ValueError, "non-empty JSON array"),
        ("[]", ValueError, "non-empty JSON array"),
        (json.dumps([{"source_id": "SAMPLE-LC-001"}]), ValueError, "missing required fields"),
    ],
    ids=["invalid-json", "not-list", "empty", "missing-required-key"],
)
def test_sample_loader_rejects_invalid_content(monkeypatch, tmp_path, content, error_type, message):
    sample_path = tmp_path / "invalid.sample.json"
    sample_path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(cli, "_SAMPLE_KB_PATH", sample_path)

    expectation = pytest.raises(error_type, match=message) if message else pytest.raises(error_type)
    with expectation:
        cli._validate_sample_data()


def test_sample_path_is_module_relative_from_external_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = _marketplace_agent(FakeLLM(), provision_first=False)

    result = _invoke_marketplace(agent, json.dumps(PREFLIGHT_PAYLOAD))

    assert result["status"] == "success"
    assert "SAMPLE-LC-001" in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("operation", ["upsert", "append_audit_event", "promote_review_point"])
def test_readonly_backend_rejects_persistence_operations(operation):
    backend = cli._ReadOnlyKBBackend()

    with pytest.raises(KBBackendError, match="Marketplace"):
        if operation == "upsert":
            backend.upsert({"metadata": {"tenant_id": TENANT}})
        elif operation == "append_audit_event":
            backend.append_audit_event({"metadata": {"tenant_id": TENANT}})
        else:
            backend.promote_review_point("SAMPLE-LC-001", tenant_id=TENANT)


def test_payload_store_rejects_other_session_and_tenant():
    agent = cli.MarketplaceGraph(config={**_file_config(), "services": {"llm": FakeLLM()}})
    store = agent.config["services"]["payload_store"]
    scope = {"tenant_id": TENANT}
    ref = store.put({"sample": True}, scope=scope, session_id="session-a")

    with pytest.raises(PayloadStoreError, match="payload_ref_scope_mismatch"):
        store.resolve(ref, scope=scope, session_id="session-b")
    with pytest.raises(PayloadStoreError, match="payload_ref_scope_mismatch"):
        store.resolve(ref, scope={"tenant_id": "other-tenant"}, session_id="session-a")


@pytest.mark.parametrize("requested_mode", ["ingest", "feedback"])
def test_pasted_write_mode_is_forced_to_preflight(monkeypatch, requested_mode):
    class TrackingBackend(cli._ReadOnlyKBBackend):
        def __init__(self) -> None:
            super().__init__()
            self.write_calls = 0

        def upsert(self, record):
            self.write_calls += 1
            return super().upsert(record)

        def append_audit_event(self, event):
            self.write_calls += 1
            return super().append_audit_event(event)

        def promote_review_point(self, source_id, *, tenant_id):
            self.write_calls += 1
            return super().promote_review_point(source_id, tenant_id=tenant_id)

    monkeypatch.setattr(cli, "_ReadOnlyKBBackend", TrackingBackend)
    agent = _marketplace_agent(FakeLLM(), provision_first=False)
    payload = {**PREFLIGHT_PAYLOAD, "mode": requested_mode}

    result = _invoke_marketplace(agent, json.dumps(payload))
    backend = agent.config["services"]["kb_backend"]

    assert result["status"] == "success"
    assert result["output"]["mode"] == "preflight"
    assert backend.write_calls == 0


def test_invoke_owns_context_session_envelope_and_payload_cleanup(monkeypatch):
    captured: dict[str, Any] = {}

    def record_parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        envelope = json.loads(user_input)
        scope = envelope["scope"]
        store = self.config["services"]["payload_store"]
        captured.update(
            envelope=envelope,
            session_id=session_id,
            context_session_id=ctx.session_id,
            hitl_allowed=ctx.hitl_allowed,
            input_context=input_context,
            payload=store.resolve(envelope["plan_draft_ref"], scope=scope, session_id=session_id),
            store=store,
        )
        return {"status": "success"}

    monkeypatch.setattr(TradeFinanceLCPreflightAgent, "invoke", record_parent_invoke)
    agent = cli.MarketplaceGraph(config={**_file_config(), "services": {"llm": FakeLLM()}})
    escaped = json.dumps({"note": "連絡先を照合"}, ensure_ascii=True)
    result = agent.invoke(
        escaped,
        session_id="caller-session",
        ctx=InvocationContext(
            session_id="runner-session",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
        input_context={"conversation_history": [{"content": "CANARY"}]},
    )

    assert result["status"] == "success"
    assert captured["envelope"]["mode"] == "preflight"
    assert captured["session_id"] == captured["context_session_id"]
    assert captured["session_id"] not in {"caller-session", "runner-session"}
    assert captured["hitl_allowed"] is False
    assert captured["input_context"] == {"configurable": {"tenant_id": TENANT}}
    assert "CANARY" not in json.dumps(captured, default=str)
    assert "連絡先" in captured["payload"]
    with pytest.raises(PayloadStoreError, match="payload_ref_unknown"):
        captured["store"].resolve(
            captured["envelope"]["plan_draft_ref"],
            scope={"tenant_id": TENANT},
            session_id=captured["session_id"],
        )


def test_invalid_payload_still_has_non_none_runner_output():
    fake_llm = FakeLLM()
    agent = _marketplace_agent(fake_llm, provision_first=False)

    result = _invoke_marketplace(agent, "not-json")

    assert result["status"] == "error"
    assert result["output"] is not None
    assert result["output"]["error_log"]
    assert fake_llm.calls == 0


def test_actual_config_installs_llm_in_services_without_replacing_tuning():
    config = _file_config()
    tuning = dict(config["llm"])

    agent = cli.MarketplaceGraph(config=config)
    installed = agent.config["services"]["llm"]

    assert isinstance(installed, cli._AzureMarketplaceLLM)
    assert agent.config["llm"] == tuning
    assert vars(installed) == {}


def test_supplied_services_llm_is_preserved():
    fake_llm = FakeLLM()
    config = _file_config()
    config["services"] = {"llm": fake_llm}

    agent = cli.MarketplaceGraph(config=config)

    assert agent.config["services"]["llm"] is fake_llm


def test_non_client_services_llm_is_replaced_using_actual_file_config():
    config = _file_config()
    tuning = dict(config["llm"])
    config["services"] = {"llm": {"temperature": 0.1}}

    agent = cli.MarketplaceGraph(config=config)

    assert isinstance(agent.config["services"]["llm"], cli._AzureMarketplaceLLM)
    assert agent.config["llm"] == tuning


def test_lazy_azure_wrapper_passes_tuning_and_keeps_no_instance_state(monkeypatch):
    captured: dict[str, Any] = {}

    class CapturingClient(FakeLLM):
        def __init__(self, config):
            super().__init__()
            captured.update(config)

    # _client() imports the SDK client inside the method and only falls back to
    # _AzureOpenAIClientCompat when that import fails. Patching the fallback alone
    # stopped intercepting anything once the wheel started shipping
    # azure_openai_client (absent in 1.0.1, present from 1.0.2) — the call then
    # reached the real client and left the process. Patch whichever one this wheel
    # actually resolves to.
    monkeypatch.setattr(cli, "_AzureOpenAIClientCompat", CapturingClient)
    try:
        azure_module = importlib.import_module("shared.services.llm.azure_openai_client")
    except ModuleNotFoundError:
        pass
    else:
        monkeypatch.setattr(azure_module, "AzureOpenAIClient", CapturingClient)
    llm = cli._AzureMarketplaceLLM(llm_tuning={"temperature": 0.1, "max_tokens": 2500, "ignored": True})
    bound = llm.bind_tools([])
    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        response = bound.complete([])

    assert response["model"] == "fake-marketplace-llm"
    assert captured["temperature"] == 0.1
    assert captured["max_tokens"] == 2500
    assert "ignored" not in captured
    assert vars(llm) == vars(bound) == {}
    assert bound is not llm


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_lazy_azure_wrapper_rejects_invalid_endpoint_before_client_import(monkeypatch, endpoint):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    llm = cli._AzureMarketplaceLLM()

    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        with pytest.raises(ValueError, match="bare"):
            llm.complete([])


def test_main_validates_environment_and_passes_file_config(monkeypatch):
    captured: dict[str, Any] = {}

    def record_runner(agent_cls, *, agent_name, namespace, config):
        captured.update(
            agent_cls=agent_cls,
            agent_name=agent_name,
            namespace=namespace,
            config=config,
            agent=agent_cls(config=config),
        )

    monkeypatch.setattr(cli, "run_agent_marketplace", record_runner)

    cli.main()

    assert captured["agent_cls"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "FIN-C2-165"
    assert captured["namespace"] == "fin"
    assert captured["config"]["llm"] == {"temperature": 0.1, "max_tokens": 2500}
    assert isinstance(captured["agent"].config["services"]["llm"], cli._AzureMarketplaceLLM)


def test_main_fails_closed_before_runner_when_sample_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_SAMPLE_KB_PATH", tmp_path / "missing.sample.json")
    called = False

    def record_runner(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "run_agent_marketplace", record_runner)

    with pytest.raises(FileNotFoundError):
        cli.main()
    assert called is False


@pytest.mark.parametrize("missing_key", cli._REQUIRED_ENV_KEYS)
def test_main_fails_closed_when_required_environment_is_missing(monkeypatch, missing_key):
    monkeypatch.delenv(missing_key)
    called = False

    def record_runner(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "run_agent_marketplace", record_runner)

    with pytest.raises(Exception):
        cli.main()
    assert called is False


def test_main_fails_closed_on_whitespace(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", " https://azure.example.invalid")
    monkeypatch.setattr(cli, "run_agent_marketplace", lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match="clean non-empty"):
        cli.main()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.setattr(cli, "run_agent_marketplace", lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match="bare"):
        cli.main()
