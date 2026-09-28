"""AGENTIC STAR Marketplace entry adapter for FIN-C2-165."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from weakref import WeakKeyDictionary

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for older AgentCore wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter.auth import STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import TradeFinanceLCPreflightAgent
from src.payload_store import mint_reference
from src.scope import SCOPE_KEYS
from src.services import InMemoryKBBackend, KBBackendError, wire_services

_NAMESPACE = "fin"
_AGENT_NAME = "FIN-C2-165"
_PROVIDER_AGENT_NAME = "FIN-C2-165"
_COMPILE_SESSION_ID = "marketplace-compile-session"
_SAMPLE_KB_PATH = Path(__file__).resolve().parent / "config" / "lc_review_points.sample.json"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_REQUIRED_ENV_KEYS = _AZURE_OPENAI_SECRET_KEYS + _SCOPE_ENV_KEYS
_ALLOWED_ENV_KEYS = frozenset(_REQUIRED_ENV_KEYS)
_LLM_TUNING_KEYS = ("temperature", "max_tokens")
_SAMPLE_REQUIRED_FIELDS = frozenset({"source_id", "title", "summary", "snippet", "metadata"})
_SAMPLE_REQUIRED_METADATA_FIELDS = frozenset({"record_type", "kb_type"})


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only deployment values consumed by this adapter."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENV_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without trimming or repair."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _require_deployment_environment(provider: SecretProvider) -> None:
    for key in _REQUIRED_ENV_KEYS:
        if key == "AZURE_OPENAI_ENDPOINT":
            _require_azure_endpoint(provider)
        else:
            _require_clean(provider, key)


def _deployment_scope(provider: SecretProvider) -> dict[str, str]:
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for pre-Marketplace AgentCore wheels."""

    def __init__(self, config: dict[str, Any], *, tools: tuple[Any, ...] = ()) -> None:
        self._config = dict(config)
        self._tools = tools

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        body: dict[str, Any] = {"messages": messages}
        for key in _LLM_TUNING_KEYS:
            if key in self._config:
                body[key] = self._config[key]
        if self._tools:
            body["tools"] = list(self._tools)
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps(body).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        return type(self)(self._config, tools=tuple(tools))


# Keep non-secret tuning/tool state outside wrapper instances. Besides ensuring
# credentials are resolved only inside each operation, this makes accidental
# instance serialization unable to retain any deployment data.
_AZURE_LLM_STATE: WeakKeyDictionary[_AzureMarketplaceLLM, tuple[dict[str, Any], tuple[Any, ...]]]


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Resolve Azure connection values and create a fresh client per operation."""

    def __init__(
        self,
        *,
        llm_tuning: Mapping[str, Any] | None = None,
        tools: tuple[Any, ...] = (),
    ) -> None:
        tuning = {
            key: llm_tuning[key] for key in _LLM_TUNING_KEYS if isinstance(llm_tuning, Mapping) and key in llm_tuning
        }
        _AZURE_LLM_STATE[self] = (tuning, tools)

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        values = {
            key: (
                _require_azure_endpoint(provider) if key == "AZURE_OPENAI_ENDPOINT" else _require_clean(provider, key)
            )
            for key in _AZURE_OPENAI_SECRET_KEYS
        }

        # Validate connection values before the optional import so configuration
        # errors are not misreported as SDK compatibility failures.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        tuning, tools = _AZURE_LLM_STATE[self]
        client_config = {
            "api_key": values["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
            **tuning,
        }
        client = cast(BaseLLM, AzureOpenAIClient(client_config))
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        tuning, _ = _AZURE_LLM_STATE[self]
        return type(self)(llm_tuning=tuning, tools=tuple(tools))


_AZURE_LLM_STATE = WeakKeyDictionary()


class _ReadOnlyKBBackend(InMemoryKBBackend):
    """Seeded retrieval backend that blocks Marketplace persistence paths."""

    def seed(self, records: list[dict[str, Any]], *, tenant_id: str) -> None:
        """Idempotently load trusted sample projections below the write guard."""

        for record in records:
            metadata = dict(record.get("metadata") or {})
            metadata["tenant_id"] = tenant_id
            super().upsert({**record, "metadata": metadata})

    def register_case(self, *, tenant_id: str, case_ref: str) -> None:
        # Stage1 calls this on every preflight. Marketplace is read-only, and the
        # return value is unused, so retain control flow without persisting.
        del tenant_id, case_ref
        return None

    def upsert(self, record: dict[str, Any]) -> str:
        del record
        raise KBBackendError("Marketplace KB writes are unavailable")

    def append_audit_event(self, event: dict[str, Any]) -> str:
        del event
        raise KBBackendError("Marketplace audit writes are unavailable")

    def promote_review_point(self, source_id: str, *, tenant_id: str) -> bool:
        del source_id, tenant_id
        raise KBBackendError("Marketplace KB promotion is unavailable")


def _load_sample_kb() -> list[dict[str, Any]]:
    payload = json.loads(_SAMPLE_KB_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("Marketplace sample KB must be a non-empty JSON array")
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise ValueError(f"Marketplace sample KB record {index} must be an object")
        missing = _SAMPLE_REQUIRED_FIELDS - set(record)
        if missing:
            raise ValueError(f"Marketplace sample KB record {index} is missing required fields: {sorted(missing)}")
        if any(
            not isinstance(record[key], str) or not record[key].strip()
            for key in ("source_id", "title", "summary", "snippet")
        ):
            raise ValueError(f"Marketplace sample KB record {index} has an invalid text field")
        metadata = record["metadata"]
        if not isinstance(metadata, dict) or not _SAMPLE_REQUIRED_METADATA_FIELDS <= set(metadata):
            raise ValueError(f"Marketplace sample KB record {index} has invalid metadata")
        if metadata.get("record_type") != "review_point" or metadata.get("kb_type") != "sample":
            raise ValueError(f"Marketplace sample KB record {index} has invalid provenance")
    return payload


def _validate_sample_data() -> None:
    _load_sample_kb()


def _seed_sample_kb(backend: _ReadOnlyKBBackend, tenant_id: str) -> None:
    backend.seed(_load_sample_kb(), tenant_id=tenant_id)


def _normalize_payload(user_input: str) -> Any:
    """Decode pasted JSON so escaped Unicode reaches the existing S-2 controls."""

    try:
        parsed = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        return user_input
    return json.dumps(parsed, ensure_ascii=False)


def _is_llm_client(candidate: object) -> bool:
    return callable(getattr(candidate, "complete", None))


def _validate_startup() -> None:
    environment_provider = _AllowlistedEnvironmentProvider()
    _require_deployment_environment(environment_provider)
    _validate_sample_data()


class MarketplaceGraph(TradeFinanceLCPreflightAgent):
    """Marketplace-only preflight boundary with read-only services."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        environment_provider = _AllowlistedEnvironmentProvider()
        trusted_scope = _deployment_scope(environment_provider)

        # Always start with the complete service set so payload_store cannot be
        # lost. A caller-supplied LLM-shaped value remains an explicit
        # test/deployment seam; all persistence services stay adapter-owned and
        # read-only.
        services = wire_services()
        supplied_services = merged.get("services")
        if isinstance(supplied_services, Mapping) and supplied_services.get("llm") is not None:
            services["llm"] = supplied_services["llm"]
        backend = _ReadOnlyKBBackend()
        _seed_sample_kb(backend, trusted_scope["tenant_id"])
        services["kb_backend"] = backend
        if not _is_llm_client(services.get("llm")):
            services["llm"] = _AzureMarketplaceLLM(llm_tuning=merged.get("llm") or {})

        merged["services"] = services
        merged["trusted_scope"] = trusted_scope
        merged["session_id"] = _COMPILE_SESSION_ID
        merged["authorized_approvers"] = []
        super().__init__(config=merged)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del session_id, input_context
        tenant_id = cast(str, self.config["trusted_scope"]["tenant_id"])
        scope = {"tenant_id": tenant_id}
        request_session_id = mint_reference()
        payload = _normalize_payload(user_input)
        payload_store = self.config["services"]["payload_store"]
        ref = payload_store.put(payload, scope=scope, session_id=request_session_id)
        envelope = json.dumps(
            {
                "mode": "preflight",
                "case_ref": mint_reference(),
                "scope": scope,
                "plan_draft_ref": ref,
            },
            ensure_ascii=False,
        )
        marketplace_ctx = replace(ctx, session_id=request_session_id, hitl_allowed=False) if ctx is not None else None
        try:
            return cast(
                dict[str, Any],
                super().invoke(
                    envelope,
                    session_id=request_session_id,
                    ctx=marketplace_ctx,
                    input_context={"configurable": scope},
                ),
            )
        finally:
            payload_store.delete(ref, scope=scope, session_id=request_session_id)

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        _require_deployment_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)

    def _enrich_output(self, output: dict[str, Any], result: dict[str, Any]) -> None:
        del result
        formatted = output.get("formatted_output")
        output["output"] = (
            formatted
            if formatted
            else {
                "status": output.get("status"),
                "error_log": output.get("error_log", []),
            }
        )


def main() -> None:
    _validate_startup()
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
