"""HTTP adapter: authenticate caller, resolve tenant scope, externalize raw
bodies to the payload-store, then invoke the agent with a checkpoint-safe
envelope.

Three domain routes (design §2-1) map onto one graph via ``mode``:
  POST /ingest    → mode "ingest"    (doc-requirement / bank-return rows)
  POST /preflight → mode "preflight" (case meta + extracted L/C + doc texts)
  POST /feedback  → mode "feedback"  (approved review points)

A fourth route, POST /invoke, is the platform/AgentGateway-compatible entry
(``InvokeRequest{input, session_id}``): ``input`` is the JSON envelope
``{"mode": ..., "payload": ..., "case_ref"?: ...}``. It dispatches on ``mode``
to the same ``_run`` path as the domain routes, so scope/auth/payload-store
externalization stay identical. Used by the STG evidence adapter and any caller
that speaks the single-endpoint contract (the STG runbook, sections 5 and 8).

Scope: this AG partitions by tenant_id ONLY (design §6). tenant_id is resolved
from verified request state (or the injected app.state.verify_scope seam), never
from client headers/body.
Raw L/C text and documents live only briefly in the payload-store; State/graph
receive an opaque payload_ref.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter import AuthContext, Operation, authenticate
from src.graph.graph import TradeFinanceLCPreflightAgent
from src.payload_store import mint_reference
from src.services import wire_services

app = FastAPI(title="Trade Finance LC Document Preflight Agent")
services = wire_services()
# L/C-officer allowlist for KB promotion (design §10-1 explicit-approval
# boundary). Sourced from the deployment env; empty = no caller may promote
# (fail-closed). The concrete IAM binding is a PoC-stage item (design §10-2).
_authorized_approvers = [a.strip() for a in os.environ.get("AUTHORIZED_APPROVERS", "").split(",") if a.strip()]
_secrets_provider = secrets_factory(namespace="fin", agent_name="FIN-C2-165")

_REF_KEYS = {
    "preflight": "plan_draft_ref",
    "ingest": "ingest_payload_ref",
    "feedback": "feedback_payload_ref",
}
_OPERATIONS = {
    # Preflight creates a shipment_packets case header in Stage1.  The shared
    # entry-adapter has no generic WRITE enum, so use its write-ingest policy:
    # both require a verified middleware identity and reject standalone/STG
    # token contexts.  Classify by side effect, not by the endpoint's name.
    "preflight": Operation.WRITE_INGEST,
    "ingest": Operation.WRITE_INGEST,
    "feedback": Operation.WRITE_FEEDBACK,
}


class PreflightRequest(BaseModel):
    payload: Any
    case_ref: str = ""
    session_id: str = ""


class IngestRequest(BaseModel):
    payload: Any
    session_id: str = ""


class FeedbackRequest(BaseModel):
    payload: Any
    case_ref: str = ""
    session_id: str = ""


class InvokeRequest(BaseModel):
    # Platform/AgentGateway single-endpoint contract: ``input`` is a JSON string
    # envelope {"mode": <ingest|preflight|feedback>, "payload": ..., "case_ref"?}.
    input: str
    session_id: str = ""


def build_agent(*, trusted_scope: dict[str, str], session_id: str) -> TradeFinanceLCPreflightAgent:
    """Build request-scoped graph nodes after entry authentication.

    ``IngestRowPipelineNode`` binds trusted scope/session in its constructor;
    sharing a compiled node and mutating that context would mix concurrent
    requests. The durable service seams remain shared, while the graph/node
    objects are request-local.
    """
    instance = TradeFinanceLCPreflightAgent(
        config={
            "services": services,
            "authorized_approvers": _authorized_approvers,
            "trusted_scope": trusted_scope,
            "session_id": session_id,
        }
    )
    instance.compile()
    instance.provision_secrets(_secrets_provider)
    return instance


def _run(mode: str, payload: Any, case_ref: str, session_id: str, request: Request) -> Any:
    try:
        operation = _OPERATIONS[mode]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Unsupported mode.") from exc
    auth: AuthContext = authenticate(request, operation=operation)
    scope = auth.scope
    tenant_id = scope["tenant_id"]
    session_id = session_id or str(uuid4())
    ref = services["payload_store"].put(
        payload, scope=scope, session_id=session_id
    )
    # A new preflight case_ref is ALWAYS server-minted (design §8-1). A
    # client-supplied case_ref is ignored here, so a caller cannot reuse an
    # existing same-tenant reference to attach a new lifecycle to it.
    # feedback targets an existing case, so its client case_ref is passed through
    # and validated against the case registry in the graph (FeedbackNode).
    if mode == "preflight":
        # mint_reference(), not uuid4(): case_ref is embedded in the envelope
        # that becomes `user_input`, one of the SDK's _PII_SCAN_FIELDS. The
        # @final S-2 gate MASKS what detect_pii matches, and a uuid4 string
        # collides with my_number_jp / credit_card / phone_jp for ~0.53% of
        # values (measured 105/20,000 on this envelope shape) — a masked
        # case_ref then fails to identify its case on the feedback path.
        generated_case_ref = mint_reference()
    else:
        generated_case_ref = case_ref
    ref_key = _REF_KEYS[mode]
    envelope = json.dumps(
        {
            "mode": mode,
            "case_ref": generated_case_ref,
            "scope": {"tenant_id": tenant_id},
            ref_key: ref,
        },
        ensure_ascii=False,
    )
    try:
        request_agent = build_agent(trusted_scope=scope, session_id=session_id)
        with bound_secrets(request_agent._secrets_provider):
            ctx = InvocationContext(
                session_id=session_id,
                caller_trust_level=auth.trust,
                caller_id=auth.principal["caller_id"],
            )
            return request_agent.invoke(
                envelope,
                ctx=ctx,
                input_context={"configurable": dict(scope)},
            )
    finally:
        services["payload_store"].delete(ref, scope=scope, session_id=session_id)


@app.post("/preflight")
async def preflight(req: PreflightRequest, request: Request):
    return _run("preflight", req.payload, req.case_ref, req.session_id, request)


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    return _run("ingest", req.payload, "", req.session_id, request)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    return _run("feedback", req.payload, req.case_ref, req.session_id, request)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    """Single-endpoint contract: parse the ``input`` envelope and dispatch on
    ``mode`` to the same ``_run`` path as the domain routes. Scope, auth, and
    payload-store externalization are unchanged — only the transport shape
    differs. An unparseable envelope or unknown mode fails closed with 400."""
    try:
        envelope = json.loads(req.input)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="input must be a JSON envelope.")
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="input envelope must be a JSON object.")
    mode = envelope.get("mode")
    # isinstance(mode, str) MUST come first: `mode not in _REF_KEYS` on an
    # unhashable value (list/dict) would raise TypeError → HTTP 500 instead of a
    # clean 400. A missing/None payload is likewise a client error, not a graph
    # error — fail closed here before _run is entered.
    if not isinstance(mode, str) or mode not in _REF_KEYS:
        raise HTTPException(status_code=400, detail="input envelope requires a valid mode.")
    if envelope.get("payload") is None:
        raise HTTPException(status_code=400, detail="input envelope requires a payload.")
    # case_ref is only honoured for feedback (preflight always server-mints
    # it); ingest has no case_ref. _run enforces this regardless of input.
    return _run(mode, envelope.get("payload"), str(envelope.get("case_ref") or ""), req.session_id, request)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "FIN-C2-165"}
