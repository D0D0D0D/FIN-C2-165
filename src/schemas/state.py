"""Checkpoint-safe state schema for the trade-finance LC preflight workflow.

Flat TypedDict only (ADR-005). LangGraph checkpoints use msgpack, so only
JSON-serializable primitives are allowed. Never place raw L/C text, shipping
documents, transaction bodies, JWT, API keys, or credentials in State — those
are held out-of-state in the payload-store and referenced by opaque id only.
"""

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    # ── routing / scope ──────────────────────────────────────────────────
    mode: str  # "ingest" / "preflight" / "feedback"
    case_ref: str  # case reference id (no raw transaction data)
    tenant_id: str  # KB partition key (fail-closed; row-level partition)
    validated_input: dict

    # ── payload references (raw bodies held out-of-state) ────────────────
    plan_draft_ref: str | None  # preflight: case meta + extracted L/C + doc texts
    ingest_payload_ref: str | None  # ingest: normalized rule / return-history rows
    feedback_payload_ref: str | None  # feedback: approved review points

    # ── Stage 1 (deterministic, immutable authoritative facts) ───────────
    docs_present: dict  # {doc_type: bool}
    field_diff: dict  # {field: {lc, doc, within_tolerance}}
    deadline_status: str  # on_time / late / at_risk
    deadline_detail: dict  # date_basis / applicable_period / days_remaining

    # ── Stage 2 (scoped KB recall) ───────────────────────────────────────
    recalled_review_points: list  # source-linked past review points
    retrieved_records: list  # scoped-retrieval standard-form records

    # ── Stage 3/4 (LLM apply + provenance) ───────────────────────────────
    preflight_brief: dict  # facts / inconsistency_candidates / bank_inquiry_points / correction_suggestions
    evidence_map: list  # claim -> source (L/C term id / doc link id)
    cited_source_ids: list
    requires_human_review: dict  # {flag: bool, reasons: [str]}
    # authoritative L/C-term / document-link source rows the LLM may cite; used
    # by Stage4's evidence-gate, then stripped from the surfaced output.
    authoritative_sources: list

    # ── ingest / feedback results ────────────────────────────────────────
    ingest_summary: dict
    feedback_result: dict

    # ── output / degradation ─────────────────────────────────────────────
    formatted_output: dict
    degradation_reason: str

    # ── inner-only aliases required by applied parts ─────────────────────
    query: str
    gate_kind: str
    draft: dict
