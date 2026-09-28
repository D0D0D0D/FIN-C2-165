"""Domain nodes composing the fixed inner LC-preflight workflow.

Modes (design §2-1):
  - preflight : Stage1 deterministic → Stage2 KB recall → Stage3 LLM apply →
                Stage4 claim→source verify.  (the outer `mode` is "preflight";
                the applied parts speak the "invoke" mode vocabulary, so the
                adapter maps preflight↔invoke at the graph boundary.)
  - ingest    : normalize doc-requirement / bank-return rows into the tenant KB.
  - feedback  : record approved review points; promote them into the KB.

Scope: this AG partitions by tenant_id ONLY (design §6, row-level partition).
All scoped parts declare that one real domain key; no dummy second axis is used.

Raw L/C text and shipping documents are never placed in State — they live in
the payload-store and are resolved (scope-checked, single-use) inside a node.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from shared.security import detect_credentials, detect_pii
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import FindingField, project_typed_finding
from src.evidence_gate.gate_node import EvidenceGateNode, ProvenanceContract
from src.ingest_row_pipeline.row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage.draft_node import DraftStageHooks, LlmDraftStageNode
from src.scoped_retrieval.retrieval_node import ScopedRetrievalNode
from src.payload_store.payload_store import PayloadStore, PayloadStoreError
from src.scope import SCOPE_KEYS, scope_for
from src.services import InMemoryKBBackend, KBBackendError

_REF_KEYS = {
    "preflight": "plan_draft_ref",
    "ingest": "ingest_payload_ref",
    "feedback": "feedback_payload_ref",
}

# Prohibited literals scanned in ingested rows (defence-in-depth; the S-3
# assertion lexicon in post_process handles user-visible output).
_PROHIBITED_ROW_TOKENS = ("password", "api_key", "secret", "token", "credential")


def _context(state: dict) -> None:
    if all(key in state for key in ("correlation_id", "session_id", "thread_id", "trace_id")):
        InvocationContext.from_state(state)


def _scope(state: dict) -> str:
    tenant_id = str(state.get("tenant_id") or "")
    if not tenant_id:
        raise SecurityViolationError("scope_missing")
    return tenant_id


def normalize_review_query(value: Any) -> str:
    """Shared normalization for KB write and retrieval (must be identical)."""
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if isinstance(value, (dict, list))
        else str(value or "")
    )
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


# ─────────────────────────────────────────────────────────────────────────────
# Envelope hydration (JSON-only GraphNode boundary → inner state)
# ─────────────────────────────────────────────────────────────────────────────


class HydrateEnvelopeNode(FunctionNode):
    """Restore the JSON-only GraphNode envelope inside the subgraph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        _context(state)
        try:
            envelope = json.loads(str(state.get("user_input", "")))
        except json.JSONDecodeError:
            envelope = None
        if not isinstance(envelope, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["inner_envelope_invalid"]}
        mode = envelope.get("mode")
        scope = envelope.get("scope") if isinstance(envelope.get("scope"), dict) else {}
        tenant_id = scope.get("tenant_id")
        ref_key = _REF_KEYS.get(mode)
        if not ref_key or not tenant_id or not envelope.get(ref_key):
            return {"status": AgentStatus.ERROR.value, "error_log": ["inner_envelope_invalid"]}
        emit_trace_event("inner_envelope_hydrated", {"mode": mode}, state)
        return {
            "mode": mode,
            "tenant_id": str(tenant_id),
            "case_ref": str(envelope.get("case_ref") or ""),
            ref_key: str(envelope[ref_key]),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — deterministic checks (LLM not used; immutable authoritative facts)
# ─────────────────────────────────────────────────────────────────────────────


class Stage1DeterministicNode(FunctionNode):
    """Docs-present / numeric field diff / presentation-deadline (design §9-1).

    Consumes the extracted (already parsed by the client) preflight payload:
      {
        "lc": {"required_docs": [...], "amount": .., "quantity": ..,
               "tolerance_pct": .., "presentation_period_days": .., "expiry": "YYYY-MM-DD"},
        "documents": {"<doc_type>": {"amount": .., "quantity": ..,
                      "on_board_date": "YYYY-MM-DD", "issue_date": "YYYY-MM-DD", ...}, ...},
        "as_of_date": "YYYY-MM-DD"   # optional; defaults to server receipt date
      }
    Numbers/dates are structured values (never raw images/PDF).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _DEFAULT_UCP600_PERIOD_DAYS = 21
    _AT_RISK_THRESHOLD_DAYS = 3

    def __init__(self, payload_store: PayloadStore | None = None, backend: InMemoryKBBackend | None = None) -> None:
        super().__init__()
        self._payload_store = payload_store or PayloadStore(scope_keys=SCOPE_KEYS)
        self._backend = backend

    def execute(self, state: dict) -> dict:
        _context(state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            tenant_id = _scope(state)
        except SecurityViolationError:
            emit_trace_event("stage1_scope_missing", {}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["scope_missing"]}
        try:
            # Stage1 reads the safe projection (structured numbers / doc presence
            # / dates). It does NOT consume the ref — Stage3 does the single-use
            # consume of the raw special-condition / document text (design §8-2).
            payload = self._payload_store.resolve(
                state.get("plan_draft_ref"),
                scope=scope_for(tenant_id),
                session_id=state["session_id"],
                consume=False,
            )
        except PayloadStoreError as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}

        parsed = payload if isinstance(payload, dict) else _loads(payload)
        if not isinstance(parsed, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["preflight_payload_unparseable"]}

        # register the case header (shipment_packets) so /feedback can validate
        # the case exists later (design §7-1 / §2-1).
        case_ref = str(state.get("case_ref") or "")
        if self._backend is not None and case_ref:
            self._backend.register_case(tenant_id=tenant_id, case_ref=case_ref)

        lc = parsed.get("lc") if isinstance(parsed.get("lc"), dict) else {}
        documents = parsed.get("documents") if isinstance(parsed.get("documents"), dict) else {}

        docs_present = _docs_present(lc, documents)
        field_diff = _field_diff(lc, documents)
        deadline_status, deadline_detail = _deadline(lc, documents, parsed.get("as_of_date"))

        # retrieval query built deterministically from structured facts only
        query = normalize_review_query(_recall_query_terms(docs_present, field_diff, deadline_status))
        # Authoritative source ids the LLM is allowed to cite in Stage3: the L/C
        # term id(s) and each document link id. Stage4's evidence-gate proves a
        # cited id exists in the union of these + the KB recall (review round-2
        # #6), so a genuine document/term citation is not treated as forged.
        authoritative_sources = _authoritative_sources(lc, documents, tenant_id)

        emit_trace_event(
            "lc_preflight_stage1_done",
            {"missing": sum(1 for v in docs_present.values() if not v), "deadline": deadline_status},
            state,
        )
        return {
            "docs_present": docs_present,
            "field_diff": field_diff,
            "deadline_status": deadline_status,
            "deadline_detail": deadline_detail,
            "authoritative_sources": authoritative_sources,
            "query": query,
            "gate_kind": "draft",
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state


_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_-]")


def _safe_id(value: Any) -> str:
    """Coerce an id to the evidence-gate safe charset [0-9A-Za-z_-]."""
    return _SAFE_ID_RE.sub("-", str(value))


def _authoritative_sources(lc: dict, documents: dict, tenant_id: str) -> list[dict[str, Any]]:
    """Build the L/C-term / document-link ids the LLM may cite (standard-form
    records so Stage4's evidence-gate accepts them alongside KB recall)."""
    sources: list[dict[str, Any]] = []
    term_id = lc.get("term_id")
    if term_id:
        sources.append(
            {"source_id": _safe_id(term_id), "source_tier": "lc_term", "metadata": {"tenant_id": tenant_id, "kind": "lc_term"}}
        )
    for doc_name, doc in documents.items():
        # prefer the document's own verified link id; fall back to a name-derived
        # id when the client did not supply one (review final #3).
        link_id = doc.get("link_id") if isinstance(doc, dict) else None
        source_id = _safe_id(link_id) if link_id else _safe_id(f"doc:{doc_name}")
        sources.append(
            {
                "source_id": source_id,
                "source_tier": "document",
                "metadata": {"tenant_id": tenant_id, "kind": "document"},
            }
        )
    return sources


# Filler words that splitting identifiers produces (`at_risk` -> "at",
# `certificate_of_origin` -> "of"). They match nearly every stored row in a
# backend that does substring matching without ranking, pushing the genuinely
# relevant recall out of top_k.
#
# Filtering by LENGTH instead would be wrong: `bl`, `awb`, `fcr`, `coo` are real
# trade-document identifiers, and a case whose only missing document is the B/L
# would produce an empty query (review round-2).
_RECALL_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "by", "for", "from", "has",
        "in", "is", "it", "its", "not", "of", "on", "or", "the", "to", "was", "with",
    }
)


def _recall_query_terms(
    docs_present: dict[str, bool], field_diff: dict[str, Any], deadline_status: Any
) -> str:
    """Build the Stage2 recall query as SEARCH WORDS, not as a JSON structure.

    The KB stores review points as free prose (``_write_ingest_row`` puts the
    normalized ``review_point`` sentence into ``summary``), and the backend
    matches by substring. Serializing the Stage1 findings with ``json.dumps``
    produced tokens like ``["insurance_certificate"]}`` which can never occur in
    that prose, so every preflight recalled zero rows while still reporting
    ``status: success`` — the accumulation loop (ingest / feedback -> Stage2)
    was write-only. Identifiers are also split into words because
    ``insurance_certificate`` does not substring-match "insurance certificate".

    Only findings that indicate a PROBLEM are used as search terms: a document
    that is present, a field within tolerance, or an on-time presentation is not
    something to recall past bank returns about.
    """
    terms: list[str] = [str(doc) for doc, present in docs_present.items() if not present]
    terms += [
        str(field)
        for field, info in field_diff.items()
        if isinstance(info, dict) and not info.get("within_tolerance")
    ]
    if str(deadline_status) in ("late", "at_risk"):
        terms.append(str(deadline_status))
    words: list[str] = []
    for term in terms:
        for word in term.replace("_", " ").replace("-", " ").split():
            # Splitting identifiers into words is what makes them match free
            # prose, but it also emits filler: `at_risk` -> "at",
            # `certificate_of_origin` -> "of". The backend matches each word by
            # substring and does not rank, so one filler word matches nearly
            # every stored row and pushes the genuinely relevant recall out of
            # top_k. Keep only words that can carry meaning on their own.
            if word.casefold() not in _RECALL_STOPWORDS:
                words.append(word)
    return " ".join(words)


def _docs_present(lc: dict, documents: dict) -> dict[str, bool]:
    required = lc.get("required_docs")
    required = required if isinstance(required, list) else []
    return {str(doc): (str(doc) in documents) for doc in required}


def _find_invoice(documents: dict) -> dict | None:
    """The commercial invoice is the authoritative source for amount/quantity
    (UCP600/ISBP). Never take the first arbitrary document that has the field
    (review #9 — packing-list quantities would otherwise win by ordering)."""
    for key, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        key_lc = str(key).casefold()
        if "invoice" in key_lc:
            return doc
    return None


def _field_diff(lc: dict, documents: dict) -> dict[str, dict[str, Any]]:
    tolerance = _to_float(lc.get("tolerance_pct"), 0.0)
    invoice = _find_invoice(documents)
    diff: dict[str, dict[str, Any]] = {}
    for field in ("amount", "quantity"):
        lc_value = _to_float(lc.get(field), None)
        if lc_value is None:
            continue
        if invoice is None or invoice.get(field) is None:
            # no authoritative invoice value -> cannot clear; flag for review
            diff[field] = {"lc": lc_value, "doc": None, "within_tolerance": False, "reason": "invoice_value_absent"}
            continue
        doc_value = _to_float(invoice.get(field), None)
        if doc_value is None:
            diff[field] = {"lc": lc_value, "doc": None, "within_tolerance": False, "reason": "invoice_value_nonnumeric"}
            continue
        allowed = abs(lc_value) * (tolerance / 100.0)
        within = abs(doc_value - lc_value) <= allowed + 1e-9
        diff[field] = {"lc": lc_value, "doc": doc_value, "within_tolerance": within}
    return diff


def _deadline(lc: dict, documents: dict, as_of_raw: Any) -> tuple[str, dict[str, Any]]:
    """Presentation-deadline per design §9-1 (UCP600-aligned).

    Basis = B/L on_board_date (fallback: B/L issue_date, note date_basis).
    applicable_period = L/C presentation_period_days if given, else 21 (UCP600).
    days_remaining = min(L/C expiry, on_board + applicable_period) - as_of_date.
    """
    bl = _find_bl(documents)
    basis_date, date_basis = None, "unknown"
    if bl:
        on_board = _to_date(bl.get("on_board_date"))
        if on_board is not None:
            basis_date, date_basis = on_board, "on_board_date"
        else:
            issue = _to_date(bl.get("issue_date"))
            if issue is not None:
                basis_date, date_basis = issue, "bl_issue_date"

    applicable_period = lc.get("presentation_period_days")
    applicable_period = (
        int(applicable_period)
        if isinstance(applicable_period, (int, float)) and applicable_period > 0
        else Stage1DeterministicNode._DEFAULT_UCP600_PERIOD_DAYS
    )
    as_of = _to_date(as_of_raw) or date.today()

    detail: dict[str, Any] = {
        "date_basis": date_basis,
        "applicable_period": applicable_period,
        "as_of_date": as_of.isoformat(),
        "days_remaining": None,
    }
    if basis_date is None:
        return "unknown", detail

    period_deadline = _add_days(basis_date, applicable_period)
    expiry = _to_date(lc.get("expiry"))
    effective_deadline = min(expiry, period_deadline) if expiry is not None else period_deadline
    days_remaining = (effective_deadline - as_of).days
    detail["days_remaining"] = days_remaining
    detail["effective_deadline"] = effective_deadline.isoformat()

    if days_remaining < 0:
        status = "late"
    elif days_remaining <= Stage1DeterministicNode._AT_RISK_THRESHOLD_DAYS:
        status = "at_risk"
    else:
        status = "on_time"
    return status, detail


def _find_bl(documents: dict) -> dict | None:
    for key, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        key_lc = str(key).casefold()
        if "bl" in key_lc or "b/l" in key_lc or "bill_of_lading" in key_lc or "lading" in key_lc:
            return doc
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — scoped KB recall (past bank-return review points)
# ─────────────────────────────────────────────────────────────────────────────


class LCRetrievalNode(ScopedRetrievalNode):
    """Part instance; adds a domain audit event on top of scoped retrieval."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        _context(state)
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            # fail-closed: the part deliberately returns NO `retrieved_records`
            # key so the caller cannot read an empty list as "searched, found
            # nothing". Do not manufacture one here.
            return result
        # Sanitize AT THE KB BOUNDARY, not at the point of display. Gating only
        # the recalls the LLM failed to cite left a hole: the prompt carried the
        # raw stored text, and a model that copied a secret into its own
        # description and cited the same source landed in `already` and skipped
        # the check entirely (review round-2 High, reproduced end-to-end).
        # Dropping the record here also removes its id from `allowed_source_ids`,
        # so a citation of it can no longer be minted downstream.
        kept: list[dict[str, Any]] = []
        suppressed = 0
        for record in result.get("retrieved_records", []) or []:
            if not isinstance(record, dict):
                continue
            safe = _safe_kb_text(str(record.get("summary") or ""))
            if safe is None:
                suppressed += 1
                continue
            kept.append({**record, "summary": safe})
        result["retrieved_records"] = kept
        result["recalled_review_points"] = [
            {"source_id": r.get("source_id"), "review_point": r.get("summary")} for r in kept
        ]
        if suppressed:
            # The officer must not silently lose past review points. The event
            # carries a count only — never the suppressed text.
            emit_trace_event("kb_recall_suppressed", {"count": suppressed, "reason": "credential"}, state)
            existing = state.get("requires_human_review")
            reasons = list(existing.get("reasons", [])) if isinstance(existing, dict) else []
            result["requires_human_review"] = {
                "flag": True,
                "reasons": sorted({*reasons, "recalled_content_suppressed:credential"}),
            }
        emit_trace_event("kb_recall_done", {"record_count": len(kept)}, state)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — LLM semantic application (UCP600/ISBP). Stage1 facts are immutable.
# ─────────────────────────────────────────────────────────────────────────────


def _stage3_prompt(state: dict) -> str:
    """Prompt hook: pass Stage1 facts as CONFIRMED (must not be re-judged), plus
    the L/C special conditions and document texts resolved from the payload store
    so the LLM can do the SEMANTIC collation the design requires (design §8-2)."""
    return json.dumps(
        {
            "instruction": (
                "You apply UCP600/ISBP as an advisory. Perform SEMANTIC collation only "
                "between the L/C special conditions and the multilingual shipping documents "
                "below. The confirmed_facts (document presence, numeric diffs, presentation "
                "deadline) are DETERMINISTIC and MUST NOT be re-judged or overridden. "
                "Do NOT state whether the bank must accept/present/pay, and do NOT assert "
                "document authenticity — surface candidates and inquiry points only. "
                "Return only a JSON object whose top-level keys are exactly the three "
                "listed in response_contract.top_level_keys; each holds an array of candidate "
                "objects whose keys are exactly response_contract.candidate_schema. A section "
                "under any other name is discarded without warning. "
                "Use claim_type=semantic for special-condition/document collation findings; "
                "each semantic finding MUST cite at least one id from allowed_source_ids. "
                "Do NOT invent ids and do NOT emit deterministic claims "
                "that contradict confirmed_facts."
            ),
            "confirmed_facts": {
                "docs_present": state.get("docs_present", {}),
                "field_diff": state.get("field_diff", {}),
                "deadline_status": state.get("deadline_status"),
                "deadline_detail": state.get("deadline_detail", {}),
            },
            # the ONLY ids a semantic finding may cite (L/C term id, per-document
            # link ids, and KB recall ids). Stage4's evidence-gate enforces this.
            "allowed_source_ids": _allowed_source_ids(state),
            "lc_special_conditions": state.get("lc_special_conditions", ""),
            "document_texts": state.get("document_texts", {}),
            "recalled_review_points": state.get("recalled_review_points", []),
            "response_contract": _draft_response_contract(),
        },
        ensure_ascii=False,
    )


def _draft_response_contract() -> dict[str, Any]:
    """Describe the response shape the assembler actually reads.

    Generated from ``DRAFT_SECTIONS`` / ``CLAIM_TYPES`` / ``CANDIDATE_KEYS`` so the
    prompt cannot drift from the reader.  Naming the candidate shape alone is not
    enough: without the section names the model invents its own, every section
    resolves to ``[]``, and the brief comes back empty under ``status: success``.
    """
    return {
        "top_level_keys": list(DRAFT_SECTIONS),
        "candidate_schema": {
            "claim_type": sorted(CLAIM_TYPES),
            "target": "field or document name this claim is about, or \"presentation\" for a deadline claim",
            "description": "advisory text; for a deterministic claim_type this is replaced by a server-generated string",
            "cited_source_ids": "array of ids taken from allowed_source_ids; required when claim_type is semantic",
        },
        "deterministic_claim_types": sorted(_DETERMINISTIC_CLAIM_TYPES),
    }


def _allowed_source_ids(state: dict) -> list[str]:
    ids = [s.get("source_id") for s in (state.get("authoritative_sources") or []) if s.get("source_id")]
    ids += [r.get("source_id") for r in (state.get("retrieved_records") or []) if r.get("source_id")]
    return sorted({str(i) for i in ids if i})


def _stage3_fallback(state: dict) -> dict[str, Any]:
    """Deterministic brief from Stage1 facts + Stage2 recall (no LLM).

    Candidates use the STRUCTURED schema (claim_type / target / cited_source_ids)
    so Stage4 verifies them by value against Stage1 facts rather than by scanning
    free text (Fable adjudication — return to the evidence_map pattern)."""
    records = state.get("retrieved_records") or []
    field_diff = state.get("field_diff") or {}
    docs_present = state.get("docs_present") or {}

    inconsistency_candidates: list[dict[str, Any]] = []
    for field, info in field_diff.items():
        if not info.get("within_tolerance"):
            inconsistency_candidates.append(
                {
                    "claim_type": "field_diff",
                    "target": field,
                    "description": f"L/C {field} and invoice {field} differ beyond tolerance.",
                    "cited_source_ids": [],
                }
            )
    for doc, present in docs_present.items():
        if not present:
            inconsistency_candidates.append(
                {
                    "claim_type": "missing_doc",
                    "target": str(doc),
                    "description": f"Required document '{doc}' appears to be missing.",
                    "cited_source_ids": [],
                }
            )

    bank_inquiry_points = []
    for record in records:
        safe = _safe_kb_text(str(record.get("summary") or "past bank return"))
        if safe is None:
            continue
        bank_inquiry_points.append(
            {
                "claim_type": "semantic",
                "target": "past_bank_return",
                "description": f"Confirm review point: {safe}",
                "cited_source_ids": [record.get("source_id")],
            }
        )
    fallback: dict[str, Any] = {name: [] for name in DRAFT_SECTIONS}
    fallback["inconsistency_candidates"] = inconsistency_candidates
    fallback["bank_inquiry_points"] = bank_inquiry_points
    return fallback


def _stage3_evidence(state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    points: list[dict[str, Any]] = []
    if isinstance(draft, dict):
        for group in DRAFT_SECTIONS:
            for item in draft.get(group, []) or []:
                if isinstance(item, dict):
                    points.append(item)
    cited = sorted(
        {str(sid) for item in points for sid in item.get("cited_source_ids", []) if sid}
    )
    evidence_map = [
        {"claim_id": str(index), "source_ids": [str(sid) for sid in item.get("cited_source_ids", []) if sid]}
        for index, item in enumerate(points)
    ]
    return evidence_map, cited


class Stage3ApplyNode(LlmDraftStageNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None, payload_store: PayloadStore | None = None) -> None:
        super().__init__(
            config=config,
            hooks=DraftStageHooks(
                build_prompt=_stage3_prompt,
                build_fallback=_stage3_fallback,
                build_evidence=_stage3_evidence,
            ),
        )
        self._payload_store = payload_store

    def execute(self, state: dict) -> dict:
        _context(state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        # Single-use consume of the raw special-condition / document text for the
        # SEMANTIC collation (design §8-2). The resolved text feeds the prompt via
        # local state only — it is NEVER returned into graph state / checkpoint.
        local = dict(state)
        if self._payload_store is not None and state.get("plan_draft_ref"):
            try:
                payload = self._payload_store.resolve(
                    state.get("plan_draft_ref"),
                    scope=scope_for(str(state.get("tenant_id") or "")),
                    session_id=state["session_id"],
                    consume=True,
                )
            except PayloadStoreError as exc:
                # Stage1 already resolved this ref (consume=False), so a scope
                # mismatch / replay / expiry here is an anomaly, not a benign
                # missing-text case — fail closed rather than silently degrade to
                # a facts-only "success" (review round-2 #5).
                return {"status": AgentStatus.ERROR.value, "error_log": [f"stage3_payload_error: {exc}"]}
            parsed = payload if isinstance(payload, dict) else _loads(payload)
            if isinstance(parsed, dict):
                lc = parsed.get("lc") if isinstance(parsed.get("lc"), dict) else {}
                local["lc_special_conditions"] = lc.get("special_conditions", lc.get("term_pattern_summary", ""))
                docs = parsed.get("documents") if isinstance(parsed.get("documents"), dict) else {}
                local["document_texts"] = {
                    k: (v.get("text", "") if isinstance(v, dict) else "") for k, v in docs.items()
                }
        result = super().execute(local)
        if result.get("status") == AgentStatus.SUCCESS.value:
            result["gate_kind"] = "draft"
        # never leak raw text back into graph state
        for key in ("lc_special_conditions", "document_texts"):
            result.pop(key, None)
        emit_trace_event("lc_apply_drafted", {"fallback": bool(result.get("degradation_reason"))}, state)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — claim→source verification + tenant boundary (evidence-gate part)
# ─────────────────────────────────────────────────────────────────────────────


_DETERMINISTIC_CLAIM_TYPES = frozenset({"field_diff", "missing_doc", "deadline"})
# Draft sections the assembler reads out of the LLM response, and the closed shape of
# one candidate.  The prompt is generated from these so it cannot announce a shape the
# assembler does not read: `draft.get(section, [])` returns an empty list for an
# unknown key, so a section named differently is dropped in silence and the run still
# reports success with an empty brief.
DRAFT_SECTIONS = ("inconsistency_candidates", "bank_inquiry_points", "correction_suggestions")
CLAIM_TYPES = frozenset(_DETERMINISTIC_CLAIM_TYPES | {"semantic"})
_CANDIDATE_FIELDS = (
    FindingField("claim_type", str, required=True, allowed_values=tuple(sorted(CLAIM_TYPES))),
    FindingField("target", str, required=True),
    FindingField("description", str, required=True),
    FindingField("cited_source_ids", list, required=True, item_type=str),
)
CANDIDATE_KEYS = tuple(field.name for field in _CANDIDATE_FIELDS)


def _reconcile_candidates(
    candidates: Any, facts: dict[str, Any], reasons: list[str], *, require_source: bool
) -> list[dict[str, Any]]:
    """Reconcile STRUCTURED candidates against Stage1 authoritative facts.

    Verification is by claim_type + target VALUE against Stage1 facts, NOT by
    scanning free text (Fable adjudication → evidence_map pattern):

    - claim_type in {field_diff, missing_doc, deadline}: the target must name a
      known Stage1 fact AND agree with it. If it does, the candidate's
      user-visible ``description`` is REPLACED by a server-generated canonical
      string derived from the fact — the LLM's own wording is discarded, so it
      cannot ship a false statement under a valid claim_type (review round-2 #2).
      A deterministic claim needs no cited source; the Stage1 fact is its source.
    - claim_type == semantic: must cite ≥1 source id proven to exist by the
      evidence-gate; an uncited semantic claim is demoted to review.
    """
    kept: list[dict[str, Any]] = []
    if not isinstance(candidates, list):
        return kept
    field_diff = facts.get("field_diff") or {}
    docs_present = facts.get("docs_present") or {}
    deadline_status = facts.get("deadline_status")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        projected = project_typed_finding(item, _CANDIDATE_FIELDS)
        claim_type = projected["claim_type"]
        target = projected["target"]
        has_source = bool([sid for sid in projected["cited_source_ids"] if sid])

        if claim_type in _DETERMINISTIC_CLAIM_TYPES:
            canonical, contradiction = _canonical_deterministic(
                claim_type, target, field_diff, docs_present, deadline_status
            )
            if contradiction:
                reasons.append(f"stage1_contradiction:{contradiction}")
                continue
            # discard the LLM's free text; emit only the server-derived string.
            kept.append(
                {
                    "claim_type": claim_type,
                    "target": target,
                    "description": canonical,
                    "cited_source_ids": [],
                }
            )
            continue

        # semantic claim: source is mandatory.
        if require_source and not has_source:
            reasons.append("uncited_semantic_claim")
            continue
        kept.append(projected)
    return kept


def _canonical_deterministic(
    claim_type: str, target: str, field_diff: dict, docs_present: dict, deadline_status: Any
) -> tuple[str, str | None]:
    """Return (canonical server-authored text, contradiction-tag-or-None).

    The canonical text is derived ONLY from Stage1 facts. A claim whose target is
    unknown, or that disagrees with the fact, returns a contradiction tag and is
    dropped by the caller.
    """
    if claim_type == "field_diff":
        info = field_diff.get(target)
        if not isinstance(info, dict):
            return "", f"field_diff_unknown_target:{target}"
        if info.get("within_tolerance") is True:
            return "", f"{target}_within_tolerance"
        return (
            f"L/C {target} ({info.get('lc')}) and invoice {target} ({info.get('doc')}) differ beyond tolerance.",
            None,
        )
    if claim_type == "missing_doc":
        present = docs_present.get(target)
        if present is None:
            return "", f"missing_doc_unknown_target:{target}"
        if present is True:
            return "", f"{target}_present"
        return f"Required document '{target}' appears to be missing from the presentation.", None
    if claim_type == "deadline":
        if not deadline_status or str(deadline_status) == "unknown":
            return "", "deadline_status_unknown"
        # target may be the generic "presentation" (per the Stage3 prompt) or the
        # actual status; anything else claims a different status and is dropped.
        if target and target not in ("presentation", str(deadline_status)):
            return "", f"deadline_status_mismatch:{target}!={deadline_status}"
        return f"Presentation deadline status is '{deadline_status}'.", None
    return "", f"unknown_claim_type:{claim_type}"


def _deterministic_findings(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """(claim_type, target) of every Stage1 fact that indicates a PROBLEM."""
    field_diff = facts.get("field_diff") or {}
    docs_present = facts.get("docs_present") or {}
    findings: list[tuple[str, str]] = []
    for field, info in field_diff.items():
        if isinstance(info, dict) and info.get("within_tolerance") is not True:
            findings.append(("field_diff", str(field)))
    for doc, present in docs_present.items():
        if present is False:
            findings.append(("missing_doc", str(doc)))
    if str(facts.get("deadline_status")) in ("late", "at_risk"):
        findings.append(("deadline", "presentation"))
    return findings


def _ensure_deterministic_candidates(
    candidates: list[dict[str, Any]], facts: dict[str, Any]
) -> list[dict[str, Any]]:
    """Prepend the Stage1 problems the reconciled candidate list does not carry.

    Descriptions come from ``_canonical_deterministic`` — the same server-authored
    strings a reconciled LLM claim would be replaced by — so no factual sentence
    is ever authored by the model.
    """
    field_diff = facts.get("field_diff") or {}
    docs_present = facts.get("docs_present") or {}
    deadline_status = facts.get("deadline_status")
    already = {
        (item.get("claim_type"), item.get("target"))
        for item in candidates
        if isinstance(item, dict)
    }
    filled: list[dict[str, Any]] = []
    for claim_type, target in _deterministic_findings(facts):
        if (claim_type, target) in already:
            continue
        canonical, contradiction = _canonical_deterministic(
            claim_type, target, field_diff, docs_present, deadline_status
        )
        if contradiction or not canonical:
            continue
        filled.append(
            {
                "claim_type": claim_type,
                "target": target,
                "description": canonical,
                # the Stage1 fact is the source of a deterministic claim
                "cited_source_ids": [],
            }
        )
    return filled + candidates


def _deterministic_review_reasons(facts: dict[str, Any]) -> list[str]:
    """Stage1 problems an L/C officer should confirm before presentation.

    ``requires_human_review`` previously only carried S-3 signals (verdict
    lexicon, uncited claims), so a presentation missing a required document and
    breaching the amount tolerance still came back ``flag: false``.
    """
    labels = {
        "missing_doc": "missing_required_document",
        "field_diff": "field_outside_tolerance",
        "deadline": "presentation_deadline",
    }
    reasons: list[str] = []
    for claim_type, target in _deterministic_findings(facts):
        if claim_type == "deadline":
            reasons.append(f"{labels[claim_type]}:{facts.get('deadline_status')}")
        else:
            reasons.append(f"{labels[claim_type]}:{target}")
    return reasons


def _ensure_recalled_review_points(
    points: list[dict[str, Any]], recalled: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append Stage2 recalls the LLM did not cite, as inquiry points.

    Text comes from the stored review point itself (KB-authored), never from the
    model. A recall the LLM already cited is not repeated.
    """
    already = {
        str(sid)
        for item in points
        if isinstance(item, dict)
        for sid in (item.get("cited_source_ids") or [])
    }
    filled: list[dict[str, Any]] = []
    for record in recalled:
        if not isinstance(record, dict):
            continue
        source_id = str(record.get("source_id") or "")
        summary = str(record.get("summary") or "").strip()
        if not source_id or not summary or source_id in already:
            continue
        safe = _safe_kb_text(summary)
        if safe is None:
            continue
        filled.append(
            {
                "claim_type": "semantic",
                "target": "past_bank_return",
                "description": f"Past bank return to confirm: {safe}",
                "cited_source_ids": [source_id],
            }
        )
    return points + filled


def _evidence_map_for_sections(sections: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """claim_id -> source ids over the FINAL candidate list (section-qualified)."""
    return [
        {
            "claim_id": f"{name}:{index}",
            "source_ids": [str(sid) for sid in (item.get("cited_source_ids") or []) if sid],
        }
        for name in DRAFT_SECTIONS
        for index, item in enumerate(sections.get(name) or [])
    ]


class Stage4VerifyNode(EvidenceGateNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self) -> None:
        super().__init__(
            contract=ProvenanceContract(
                passthrough_keys=(
                    "draft",
                    "degradation_reason",
                    "requires_human_review",
                    "docs_present",
                    "field_diff",
                    "deadline_status",
                    "deadline_detail",
                )
            )
        )

    def execute(self, state: dict) -> dict:
        _context(state)
        # Merge Stage1's authoritative L/C-term / document-link sources into the
        # record set the evidence-gate validates against, so a genuine document
        # or term citation is accepted (review round-2 #6).
        local = dict(state)
        merged = list(state.get("retrieved_records") or [])
        merged.extend(state.get("authoritative_sources") or [])
        local["retrieved_records"] = merged
        result = super().execute(local)
        if "gate_kind" not in result and result.get("status") != AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value, "error_log": ["provenance_input_missing"]}
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
        facts = {
            "docs_present": state.get("docs_present", {}),
            "field_diff": state.get("field_diff", {}),
            "deadline_status": state.get("deadline_status"),
            "deadline_detail": state.get("deadline_detail", {}),
        }
        reasons: list[str] = []
        # Assemble the preflight brief. Deterministic claims (field_diff /
        # missing_doc / deadline) are verified by value against Stage1 facts;
        # semantic claims must cite an existing source. Contradictions are
        # dropped, uncited semantics demoted (review round-2 #4/#5).
        sections = {
            name: _reconcile_candidates(draft.get(name, []), facts, reasons, require_source=True)
            for name in DRAFT_SECTIONS
        }
        # A Stage1 finding must not depend on the LLM choosing to enumerate it.
        # The Stage3 prompt steers the model to `semantic` findings, so a missing
        # document or an out-of-tolerance amount appeared in `facts` only and was
        # absent from the candidate list the officer reads — while the LLM-failure
        # fallback DID list them. Fill them in deterministically here.
        sections["inconsistency_candidates"] = _ensure_deterministic_candidates(
            sections["inconsistency_candidates"], facts
        )
        # Same reasoning for Stage2: the recall is handed to the LLM in the
        # prompt, but a model that does not cite it makes the accumulated
        # knowledge invisible to the officer — the fallback path DID list it.
        sections["bank_inquiry_points"] = _ensure_recalled_review_points(
            sections["bank_inquiry_points"], state.get("retrieved_records") or []
        )
        # Defense in depth: whatever route a sentence took to get here, it must
        # not carry a credential into a customer-visible field. The framework
        # S-3 gate cannot see it — it only scans top-level strings of the result
        # and this lives inside `preflight_brief`.
        dropped = 0
        for name in DRAFT_SECTIONS:
            safe_items = []
            for item in sections[name]:
                if _credential_findings(str(item.get("description") or "")):
                    dropped += 1
                    continue
                safe_items.append(item)
            sections[name] = safe_items
        if dropped:
            emit_trace_event("candidate_suppressed", {"count": dropped, "reason": "credential"}, state)
            reasons.append("candidate_content_suppressed:credential")
        result["preflight_brief"] = {"facts": facts, **sections}
        # claim_id has to address the FINAL candidate list. Stage3 built the map
        # from the raw draft; reconciliation drops contradictions and the step
        # above prepends deterministic findings, so positional ids no longer
        # pointed at the claim they were minted for.
        result["evidence_map"] = _evidence_map_for_sections(sections)
        # `cited_source_ids` is the same statement in another shape; leaving the
        # Stage3 value behind makes them disagree (an uncited-by-the-LLM recall
        # appears in the map but not in the list, and the audit event counts the
        # stale one).
        result["cited_source_ids"] = sorted(
            {sid for entry in result["evidence_map"] for sid in entry["source_ids"]}
        )
        reasons.extend(_deterministic_review_reasons(facts))
        rhr = state.get("requires_human_review")
        rhr = dict(rhr) if isinstance(rhr, dict) else {"flag": False, "reasons": []}
        if reasons:
            existing = rhr.get("reasons") if isinstance(rhr.get("reasons"), list) else []
            rhr = {"flag": True, "reasons": sorted(set(existing) | set(reasons))}
        result["requires_human_review"] = rhr
        result["tenant_id"] = state.get("tenant_id")
        # remember the real KB recall so the output surfaces only those records,
        # not the synthetic term/document source rows used for gate validation.
        result["_kb_records"] = list(state.get("retrieved_records") or [])
        emit_trace_event(
            "claim_source_verified",
            {"cited_count": len(result.get("cited_source_ids", [])), "review": rhr.get("flag")},
            state,
        )
        return result

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        expected_tenant = result.get("tenant_id")
        if not expected_tenant:
            raise SecurityViolationError("provenance_scope_missing")
        for record in result.get("retrieved_records", []):
            metadata = record.get("metadata") if isinstance(record, dict) else None
            if not isinstance(metadata, dict) or not metadata.get("tenant_id"):
                raise SecurityViolationError("provenance_scope_missing")
            if metadata["tenant_id"] != expected_tenant:
                raise SecurityViolationError("provenance_scope_mismatch")
        # surface only the real KB recall, not the synthetic term/doc source rows
        if "_kb_records" in result:
            result["retrieved_records"] = result.pop("_kb_records")
        result.pop("tenant_id", None)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# ingest mode — normalize doc-requirement / bank-return rows into tenant KB
# ─────────────────────────────────────────────────────────────────────────────


def _sanitize_ingest_row(state: dict, row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "record_id",
        "record_type",
        "observed_at",
        "term_id",
        "return_id",
        "doc_requirement",
        "term_pattern_summary",
        "review_point",
        "inquiry_reason",
        "quality_flag",
    }
    cleaned = {key: value for key, value in row.items() if key in allowed}
    text = " ".join(str(value) for value in cleaned.values())
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if any(token in normalized for token in _PROHIBITED_ROW_TOKENS):
        raise SecurityViolationError("prohibited_content")
    # The token list above only catches rows that NAME a secret ("password",
    # "api_key"). A row carrying an actual credential value passes it, and the
    # framework S-3 gate will not see it either once it is nested inside the
    # brief. Reject at the door; the reason must not echo the value back.
    # Check each stored free-text field on its own. Concatenating them first
    # manufactures signatures that exist in no single field ("Bearer" in one
    # column + an opaque id in the next) and rejects legitimate rows.
    for field in ("review_point", "term_pattern_summary", "inquiry_reason"):
        value = cleaned.get(field)
        if isinstance(value, str) and _credential_findings(value):
            raise SecurityViolationError("credential_in_row")
    return cleaned


def _credential_findings(text: str) -> list[dict[str, Any]]:
    """Credential detection with the `bearer_token` class filtered out.

    `detect_credentials` documents that `bearer_token` matches any sufficiently
    long "Bearer <token>" string and tells callers with known-safe bearer
    wording to post-filter it. Trade finance is exactly that case — a bearer
    bill of lading is ordinary business language, and rejecting it would keep
    legitimate past review points out of the KB (measured: "Bearer
    BILL1234567890123456 must be endorsed" was flagged).

    The trade-off is explicit: a genuine HTTP bearer token pasted into a review
    point is not caught by this class. Every other credential class (API keys,
    JWTs, AWS keys, connection strings) still blocks.
    """
    return [f for f in detect_credentials(text) if f.get("type") != "bearer_token"]


def _safe_kb_text(summary: str) -> str | None:
    """Gate free text crossing the KB boundary — on the way IN and on the way OUT.

    Applied at WRITE time so the KB never accumulates the raw value: the
    established pattern for an accumulation agent is to derive the
    model-visible value once,
    at write (Professional Services Engagement Acceptance Evidence Agent's `decision_rationale_safe` / the `accumulation-ledger`
    part's 3-value split), not to re-mask on every read. Masking only on read
    leaves the raw text in the store, where a DB dump, a checkpoint, or one
    overlooked read path exposes every row ever ingested.

    It is applied at READ time as well, as defence in depth for rows that
    entered before this gate existed.

    The framework cannot do either for us: S-2 scans `user_input` /
    `validated_input` / `llm_response`, and an ingest row reaches the graph as a
    payload-store reference, so it never crosses that gate. S-3 only scans
    TOP-LEVEL string values of the result dict
    (`FunctionNode._security_gate_output`, verified against the installed
    wheel), and `preflight_brief` / `formatted_output` are nested dicts.

    credential  -> drop the whole sentence (never partially redact a secret)
    contact PII -> mask in place

    ``name`` findings are deliberately ignored: the detector matches any two
    consecutive Title Case words, so masking them destroys ordinary business
    prose ("Bank Return", "Commercial Invoice") without protecting anyone.
    """
    if _credential_findings(summary):
        return None
    spans: list[tuple[int, int, str]] = sorted(
        (f["start"], f["end"], f["type"]) for f in detect_pii(summary) if f["type"] != "name"
    )
    # Overlapping findings are real: "0900-1234-5678-9012" matches both
    # credit_card and phone_jp. Replacing each span independently corrupts the
    # string ("[phone_jp masked]asked]"), so merge overlaps first.
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_kind = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_kind if prev_kind == kind else "pii")
            continue
        merged.append((start, end, kind))
    masked = summary
    for start, end, kind in reversed(merged):
        masked = f"{masked[:start]}[{kind} masked]{masked[end:]}"
    return masked


def _write_ingest_row(backend: InMemoryKBBackend, state: dict, row: dict[str, Any]) -> str:
    tenant_id = _scope(state)
    record_type = str(row.get("record_type") or "review_point")
    source_id = str(row.get("record_id") or row.get("term_id") or row.get("return_id") or f"rec-{len(backend.records) + 1}")
    raw_summary = str(row.get("review_point") or row.get("term_pattern_summary") or row.get("inquiry_reason") or "")
    summary = _safe_kb_text(raw_summary)
    snippet = _safe_kb_text(str(row.get("inquiry_reason") or ""))
    if summary is None or snippet is None:
        # `_sanitize_ingest_row` already rejects these at the door; reaching
        # here means a caller bypassed the sanitize hook, so fail rather than
        # store the value.
        raise SecurityViolationError("credential_in_row")
    return backend.upsert(
        {
            "source_id": source_id,
            "title": record_type,
            "summary": summary,
            "snippet": snippet,
            "metadata": {
                "tenant_id": tenant_id,
                "record_type": record_type,
                "quality_flag": str(row.get("quality_flag") or "normal"),
                # bank-return review points enter the KB directly (design §7-3);
                # feedback-originated points require kb_promoted (§10-1).
                "kb_promoted": True,
            },
        }
    )


class IngestRowsNode(IngestRowPipelineNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields = ("observed_at",)
    # v1 ingests past bank-return REVIEW POINTS only. A row that carries a
    # `doc_requirement` rule (but no review point) is explicitly rejected with a
    # reason rather than stored-but-ignored: the lc_terms_registry rule store
    # that Stage1 would consult is not wired in v1 (design §7-2 deferred), so
    # silently accepting the rule would be a no-op success (Fable adjudication /
    # review #10). The preflight caller passes L/C terms in the request payload.
    required_any_fields = (("review_point", "term_pattern_summary", "inquiry_reason"),)

    def __init__(
        self,
        payload_store: PayloadStore | None = None,
        backend: InMemoryKBBackend | None = None,
        *,
        trusted_scope: dict[str, str],
        session_id: str,
    ) -> None:
        payload_store = payload_store or PayloadStore(scope_keys=SCOPE_KEYS)
        backend = backend or InMemoryKBBackend()
        super().__init__(
            payload_store=payload_store,
            trusted_scope=trusted_scope,
            session_id=session_id,
            hooks=RowPipelineHooks(
                sanitize=_sanitize_ingest_row,
                normalize=lambda _state, row: {**row, "review_point": normalize_review_query(row.get("review_point", ""))}
                if row.get("review_point")
                else row,
                write=lambda state, row: _write_ingest_row(backend, state, row),
            ),
        )
    def execute(self, state: dict) -> dict:
        _context(state)
        result = super().execute({**state, "payload_ref": state.get("ingest_payload_ref")})
        result["gate_kind"] = "ingest"
        emit_trace_event(
            "lc_rows_ingested", {"written": result.get("ingest_summary", {}).get("written_count", 0)}, state
        )
        return result


# ─────────────────────────────────────────────────────────────────────────────
# feedback mode — record approved review points; promote to KB (design §10-1)
# ─────────────────────────────────────────────────────────────────────────────


class FeedbackNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        payload_store: PayloadStore | None = None,
        backend: InMemoryKBBackend | None = None,
        authorized_approvers: Any = None,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store or PayloadStore(scope_keys=SCOPE_KEYS)
        self._backend = backend or InMemoryKBBackend()
        # Callers authorized to promote a review point into the shared KB
        # (the L/C-officer allowlist). Sourced from config; empty/unset means
        # NO caller may promote (fail-closed). The concrete IAM binding is a
        # PoC-stage item (design §10-2), but the authorization GATE ships now so
        # promotion can never be self-authorized from the payload.
        self._authorized_approvers = frozenset(str(a) for a in (authorized_approvers or ()))

    def execute(self, state: dict) -> dict:
        _context(state)
        try:
            tenant_id = _scope(state)
        except SecurityViolationError:
            emit_trace_event("feedback_scope_missing", {}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["scope_missing"]}
        try:
            payload = self._payload_store.resolve(
                state.get("feedback_payload_ref"),
                scope=scope_for(tenant_id),
                session_id=state["session_id"],
                consume=True,
            )
        except PayloadStoreError as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}

        feedback = payload if isinstance(payload, dict) else {}
        case_ref = str(feedback.get("case_ref") or state.get("case_ref") or "")
        event_type = str(feedback.get("event_type") or "")
        # The approver is the VERIFIED caller identity, never the payload's
        # self-reported approved_by — otherwise any authenticated tenant caller
        # could forge an approver name and self-authorize KB promotion.
        approved_by = str(state.get("caller_id") or "")
        # event_id is REQUIRED so a resend is idempotent (design §2-1 "同一イベント
        # 再送は冪等"). Without it every resend would append a new audit row
        # (review round-2 #7). The client (front-end) supplies a stable id.
        client_event_id = str(feedback.get("event_id") or "")
        if (
            event_type not in {"point_confirmed", "point_edited", "kb_promoted"}
            or not case_ref
            or not approved_by
            or not client_event_id
        ):
            return {"status": AgentStatus.ERROR.value, "error_log": ["invalid_feedback"]}

        # feedback against a non-existent case is an error (design §2-1).
        if not self._backend.case_exists(tenant_id=tenant_id, case_ref=case_ref):
            emit_trace_event("feedback_unknown_case", {"case_ref": case_ref}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["case_not_found"]}

        # KB promotion requires the verified caller to hold approval authority
        # (L/C-officer allowlist). Fail closed: an unlisted caller cannot promote
        # (design §10-1 explicit-approval boundary).
        if event_type == "kb_promoted" and approved_by not in self._authorized_approvers:
            emit_trace_event("feedback_promotion_denied", {"case_ref": case_ref}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["promotion_not_authorized"]}

        try:
            event_id = self._backend.append_audit_event(
                {
                    "event_id": client_event_id,
                    "case_ref": case_ref,
                    "event_type": event_type,
                    "approved_by": approved_by,
                    "metadata": {"tenant_id": tenant_id},
                }
            )
        except KBBackendError as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}

        kb_updated = False
        # KB write-back happens ONLY on authorized explicit approval. §10-1.
        promote_id = feedback.get("source_id")
        if event_type == "kb_promoted" and promote_id:
            kb_updated = self._backend.promote_review_point(str(promote_id), tenant_id=tenant_id)

        emit_trace_event("feedback_recorded", {"event_type": event_type, "kb_updated": kb_updated}, state)
        return {
            "feedback_result": {"case_ref": case_ref, "recorded_events": [event_id], "kb_updated": kb_updated},
            "gate_kind": "feedback",
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state


# ─────────────────────────────────────────────────────────────────────────────
# small parsing helpers
# ─────────────────────────────────────────────────────────────────────────────


def _loads(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _rows_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        records = payload.get("records")
        return records if isinstance(records, list) else None
    parsed = _loads(payload)
    if parsed is payload:
        return None
    return _rows_from_payload(parsed) if parsed is not None else None


def _to_float(value: Any, default: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _add_days(base: date, days: int) -> date:
    from datetime import timedelta

    return base + timedelta(days=days)
