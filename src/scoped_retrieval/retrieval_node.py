# PART: scoped-retrieval v0.3.0 (parts@01ca509)
"""Scope-keyed retrieval node.

Backends are injected. This part owns scope fail-closed behavior, query
normalization seam, safe projection, and the `retrieved_records` contract.

Scope contract (v0.3.0): the template declares its own KB partition keys with
domain-native names via ``scope_keys`` (e.g. ``("production_company_id",
"project_id")``). All declared keys must be present in state per-request as
non-empty strings; anything else fails closed with ``status=error`` and no
backend call. Every hit must also contain every declared key at top level or
in metadata; missing keys, conflicting copies, and values that disagree with
the request are rejected. Scope values in projected metadata are the trusted
request values — backend-returned copies are never passed through.
"""

from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from typing import Any, Callable, ClassVar, Mapping, Protocol, Sequence

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


NormalizeQuery = Callable[[Any], str]


class RetrievalBackend(Protocol):
    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]: ...


TOP_LEVEL_SAFE_KEYS = frozenset({"score", "title", "summary", "snippet", "url", "published_date", "kind"})
METADATA_SAFE_KEYS = frozenset(
    {
        "record_id",
        "source_id",
        "source_tier",
        "source_doc",
        "chunk_hash",
        "kb_type",
        "category",
        "record_type",
        "published_date",
        "url",
        "title",
        "kind",
    }
)
MAX_SAFE_METADATA_CHARS = 64


class ScopedRetrievalNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        scope_keys: Sequence[str],
        backend: RetrievalBackend | None = None,
        normalize_query: NormalizeQuery | None = None,
        top_k: int = 8,
        allow_raw_query: bool = False,
    ) -> None:
        super().__init__()
        self._scope_keys = _validate_scope_keys(scope_keys)
        if backend is not None and normalize_query is None and not allow_raw_query:
            raise ValueError(
                "ScopedRetrievalNode: normalize_query hook is required when backend is configured. "
                "Use the same function as the write path; allow_raw_query=True is for tests only."
            )
        self._backend = backend
        self._normalize_query = normalize_query or (lambda value: str(value or ""))
        self._top_k = top_k

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        invalid = [key for key in self._scope_keys if not _valid_scope_value(state.get(key))]
        if invalid:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ScopedRetrievalNode: scope keys missing or invalid: {invalid} (fail closed)"],
            }
        scope = {key: state[key] for key in self._scope_keys}

        query_source = state.get("query")
        if query_source is None:
            query_source = state.get("structured_query") or state.get("_stage1_record") or state.get("validated_input")
        query = self._normalize_query(query_source)
        if not query.strip():
            return {
                "retrieved_records": [],
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": "retrieval skipped: normalized query is empty",
            }

        if self._backend is None:
            return {
                "retrieved_records": [],
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": "retrieval backend not configured",
            }

        hits = self._backend.search(scope=scope, query=query, top_k=self._top_k)
        retrieved: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, hit in enumerate(hits or []):
            mismatched = _scope_mismatch_keys(hit, scope)
            if mismatched:
                rejected.append({"index": index, "reason": f"scope mismatch: {mismatched}"})
                continue
            projected = normalize_retrieved_record(hit, expected_scope=scope)
            if projected is None:
                rejected.append({"index": index, "reason": "missing id/source_id"})
                continue
            retrieved.append(projected)

        result: dict[str, Any] = {
            "retrieved_records": retrieved,
            "retrieval_rejected": rejected,
            "status": AgentStatus.SUCCESS.value,
        }
        if not retrieved:
            result["degradation_reason"] = "no scoped retrieval hits"
        emit_trace_event(
            "scoped_retrieval_complete",
            {"scope": scope, "hit_count": len(retrieved), "rejected_count": len(rejected)},
            state,
        )
        return result

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state


def normalize_retrieved_record(
    hit: dict[str, Any],
    *,
    expected_scope: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Project a backend hit onto the safe `retrieved_records` shape.

    When ``expected_scope`` is provided, the hit must contain every declared
    key and all top-level/metadata copies must match before projection. Scope
    values in the output metadata are then the trusted ``expected_scope``
    values, never backend-returned copies — so direct callers cannot relabel
    an unscoped or mismatched hit, and declaring a scope key can never widen
    the metadata allowlist for backend content.
    """
    if expected_scope and _scope_mismatch_keys(hit, expected_scope):
        return None
    if not isinstance(hit, dict):
        return None
    source_id = hit.get("source_id") or hit.get("id") or hit.get("record_id")
    if _missing(source_id):
        return None

    scope = dict(expected_scope or {})
    dropped_keys: set[str] = set()
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    safe_metadata = _project_metadata(metadata, dropped_keys)
    safe_metadata.update(scope)
    dropped_keys -= set(scope)
    record = {
        "id": str(source_id),
        "source_id": str(source_id),
        "source_tier": hit.get("source_tier") or safe_metadata.get("source_tier") or "internal_kb",
        "metadata": safe_metadata,
    }
    for key in TOP_LEVEL_SAFE_KEYS:
        if hit.get(key) is not None:
            record[key] = hit[key]
    for key, value in hit.items():
        if key in {"source_id", "id", "record_id", "source_tier", "metadata"} or key in TOP_LEVEL_SAFE_KEYS:
            continue
        if key in scope:
            continue
        dropped_keys.add(key)
    if dropped_keys:
        record["dropped_keys"] = sorted(dropped_keys)
    return record


def _validate_scope_keys(scope_keys: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(scope_keys, SequenceABC) or isinstance(scope_keys, (str, bytes)):
        raise ValueError(
            "ScopedRetrievalNode: scope_keys must be a sequence of key names (tuple/list), not a bare string"
        )
    keys = tuple(scope_keys)
    if not keys:
        raise ValueError(
            "ScopedRetrievalNode: scope_keys must declare at least one key. "
            "If this domain has no partition inside one customer's KB, do not use this part."
        )
    for key in keys:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise ValueError(
                f"ScopedRetrievalNode: scope_keys entries must be non-empty identifier strings: {list(keys)!r}"
            )
    if len(set(keys)) != len(keys):
        raise ValueError(f"ScopedRetrievalNode: scope_keys contains duplicates: {list(keys)}")
    return keys


def _valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _scope_mismatch_keys(hit: Any, scope: Mapping[str, str]) -> list[str]:
    """Return scope key names (names only, no values) the hit lacks or disagrees on."""
    if not isinstance(hit, dict):
        return list(scope)
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    mismatched = []
    for key, expected in scope.items():
        containers = [container for container in (hit, metadata) if key in container]
        if not containers or any(str(container[key]) != expected for container in containers):
            mismatched.append(key)
    return mismatched


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _project_metadata(metadata: dict[str, Any], dropped_keys: set[str]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in METADATA_SAFE_KEYS:
            dropped_keys.add(key)
            continue
        if isinstance(value, str) and not _safe_string_shape(value):
            dropped_keys.add(key)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        else:
            dropped_keys.add(key)
    return safe


def _safe_string_shape(value: str) -> bool:
    if "\n" in value or "\r" in value:
        return False
    return len(value) <= MAX_SAFE_METADATA_CHARS
