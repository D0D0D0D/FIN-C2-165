"""Local service seams used by nodes; production clients are wired at startup.

This module provides *injectable* seams only — no model ids, no concrete DB or
LLM client. Deployments replace these with real clients via ``wire_services``.

Data governance (design §7): each KB record separates an audit-only body (raw
transaction/document text, held out-of-state via the payload-store) from a
retrieval-safe projection (review points, term ids, doc-requirement rules). Only
the retrieval-safe projection is indexed and made visible to retrieval/LLM.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

class KBBackendError(ValueError):
    """Raised when a KB write is attempted without a valid tenant scope."""


class InMemoryKBBackend:
    """Injectable KB seam with a mandatory tenant partition filter.

    Backs the retrieval-safe projections of ``lc_terms_registry`` and
    ``bank_return_history`` (design §7-2 / §7-3). ``feedback_audit_events`` is an
    append-only audit log; only ``kb_promoted`` review points are surfaced to
    retrieval. Real deployments swap this for a partitioned SQL/vector store.
    """

    def __init__(self) -> None:
        # retrieval-safe records (indexed / LLM-visible)
        self.records: list[dict[str, Any]] = []
        # append-only feedback audit log (audit-only; not indexed)
        self.audit_events: list[dict[str, Any]] = []
        # shipment_packets case headers (design §7-1), keyed by (tenant, case_ref)
        self.cases: dict[tuple[str, str], dict[str, Any]] = {}

    # ── case header (shipment_packets, design §7-1) ──────────────────────
    def register_case(self, *, tenant_id: str, case_ref: str) -> None:
        if not tenant_id or not case_ref:
            raise KBBackendError("scope_missing")
        self.cases.setdefault((tenant_id, case_ref), {"window_status": "preflighted"})

    def case_exists(self, *, tenant_id: str, case_ref: str) -> bool:
        return (tenant_id, case_ref) in self.cases

    # ── write side (ingest) ──────────────────────────────────────────────
    def upsert(self, record: dict[str, Any]) -> str:
        metadata = record.get("metadata") or {}
        tenant_id = metadata.get("tenant_id")
        if not tenant_id:
            raise KBBackendError("scope_missing")
        source_id = str(record.get("source_id") or uuid4())
        # Replace within the SAME tenant partition only — a
        # colliding source_id from another tenant must never delete this
        # tenant's record (cross-tenant overwrite).
        self.records = [
            item
            for item in self.records
            if not (
                item.get("source_id") == source_id
                and (item.get("metadata") or {}).get("tenant_id") == tenant_id
            )
        ]
        self.records.append({**record, "source_id": source_id})
        return source_id

    # ── read side (scoped-retrieval backend contract) ────────────────────
    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        # fail-closed: the part already guards, but never scan on empty scope.
        tenant_id = scope.get("tenant_id") if isinstance(scope, Mapping) else None
        if not tenant_id or set(scope) != {"tenant_id"}:
            return []
        terms = set(query.casefold().split())
        matches: list[dict[str, Any]] = []
        for record in self.records:
            metadata = record.get("metadata") or {}
            if metadata.get("tenant_id") != tenant_id:
                continue
            # low-quality records are de-weighted, not excluded (design §7-3).
            quality = 1 if metadata.get("quality_flag") == "low" else 2
            text = " ".join(str(record.get(key, "")) for key in ("title", "summary", "snippet")).casefold()
            score = sum(term in text for term in terms) * quality
            matches.append({**record, "score": score})
        matches = [m for m in matches if m["score"] > 0]
        return sorted(matches, key=lambda item: item["score"], reverse=True)[:top_k]

    # ── feedback audit log (append-only, idempotent by event_id) ─────────
    def append_audit_event(self, event: dict[str, Any]) -> str:
        metadata = event.get("metadata") or {}
        tenant_id = metadata.get("tenant_id")
        if not tenant_id:
            raise KBBackendError("scope_missing")
        event_id = str(event.get("event_id") or uuid4())
        # idempotent replay: a repeated (tenant, org, event_id) is a no-op that
        # returns the existing id rather than appending a duplicate (review #7).
        for existing in self.audit_events:
            emeta = existing.get("metadata") or {}
            if (
                existing.get("event_id") == event_id
                and emeta.get("tenant_id") == tenant_id
            ):
                return event_id
        self.audit_events.append({**event, "event_id": event_id})
        return event_id

    def promote_review_point(self, source_id: str, *, tenant_id: str) -> bool:
        """Flip a review point to kb_promoted so Stage2 can recall it."""
        for record in self.records:
            metadata = record.get("metadata") or {}
            if (
                record.get("source_id") == source_id
                and metadata.get("tenant_id") == tenant_id
            ):
                metadata["kb_promoted"] = True
                record["metadata"] = metadata
                return True
        return False


class SQLiteKBBackend(InMemoryKBBackend):
    """SQLite implementation used to exercise the real scoped query boundary.

    The local runtime still uses the injectable in-memory seam, while this
    backend proves the production protocol against an actual SQL ``WHERE``.
    Returned scope is selected from the stored row, never copied from the
    request mapping.
    """

    def __init__(self, database: str = ":memory:") -> None:
        super().__init__()
        self._db = sqlite3.connect(database)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS review_points (
                tenant_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                snippet TEXT NOT NULL,
                record_type TEXT NOT NULL,
                quality_flag TEXT NOT NULL,
                PRIMARY KEY (tenant_id, source_id)
            )"""
        )

    def upsert(self, record: dict[str, Any]) -> str:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        tenant_id = metadata.get("tenant_id")
        if not tenant_id:
            raise KBBackendError("scope_missing")
        source_id = str(record.get("source_id") or uuid4())
        self._db.execute(
            """INSERT OR REPLACE INTO review_points
               (tenant_id, source_id, title, summary, snippet, record_type, quality_flag)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                tenant_id,
                source_id,
                str(record.get("title") or ""),
                str(record.get("summary") or ""),
                str(record.get("snippet") or ""),
                str(metadata.get("record_type") or "review_point"),
                str(metadata.get("quality_flag") or "normal"),
            ),
        )
        self._db.commit()
        return source_id

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        tenant_id = scope.get("tenant_id") if isinstance(scope, Mapping) else None
        if not tenant_id or set(scope) != {"tenant_id"}:
            return []
        terms = [term for term in query.casefold().split() if term]
        if not terms:
            return []
        clauses = " OR ".join("lower(title || ' ' || summary || ' ' || snippet) LIKE ?" for _ in terms)
        rows = self._db.execute(
            f"""SELECT tenant_id, source_id, title, summary, snippet, record_type, quality_flag
                FROM review_points
                WHERE tenant_id = ? AND ({clauses})
                LIMIT ?""",
            (tenant_id, *(f"%{term}%" for term in terms), top_k),
        ).fetchall()
        return [
            {
                "source_id": source_id,
                "title": title,
                "summary": summary,
                "snippet": snippet,
                "metadata": {
                    "tenant_id": stored_tenant,
                    "record_type": record_type,
                    "quality_flag": quality_flag,
                },
            }
            for stored_tenant, source_id, title, summary, snippet, record_type, quality_flag in rows
        ]


def wire_services() -> dict[str, Any]:
    """Create runtime service seams; deployments replace these with real clients.

    ``llm`` is ``None`` here so nodes take their deterministic fallback path in
    tests and local runs. A deployment injects a ``shared.services.llm.BaseLLM``
    via config — the template never names a model id or concrete client.
    """
    from src.payload_store.payload_store import PayloadStore

    return {
        "payload_store": PayloadStore(scope_keys=("tenant_id",)),
        "kb_backend": InMemoryKBBackend(),
        "llm": None,
    }
