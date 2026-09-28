# FIN-C2-165 — Trade Finance LC Document Preflight Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: FIN

## Overview

This agent gives a trade-finance team a preflight read on a letter-of-credit presentation: it
compares the L/C terms against the shipping documents — commercial invoice, packing list, bill of
lading — and returns the inconsistencies worth looking at, the points that may need a query to the
bank, and candidate corrections, each linked to the clause or document it came from.

It is a preflight, not an examination. There is no accept/reject field, no payment decision, and
no authenticity check anywhere in the output schema; those belong to the bank and to the operator.

The work is split so that the facts are computed and only the reading of them is generated:

- **Stage 1 is deterministic.** Required-document presence, numeric matching within the L/C's
  stated tolerance (not exact equality), and the presentation deadline — computed from the on-board
  date, the L/C's presentation period or the UCP 600 default of 21 days, and the request's
  reference date — produce immutable facts with no model involvement.
- **Stage 2 recalls what the bank returned before.** Prior return and query reasons for the same
  tenant are retrieved from the accumulated knowledge base and carried into the review points.
- **Stage 3 is the only generated part**, a semantic comparison informed by UCP 600 and ISBP.
- **Stage 4 does not trust the model to have remembered the facts.** Deterministic findings
  (missing document, out-of-tolerance field, deadline status) and recalled review points are merged
  into the output mechanically, from the server-side records themselves. Measured behaviour is that
  a real model concentrates on the semantic comparison and often reports neither — without this
  step, the degraded path would return a *richer* candidate list than the healthy one.

Statements of fact in the output are rendered from stored records, never authored by the model.
Every claim must resolve to a real Stage 1 or Stage 2 source; a fabricated or cross-tenant source
identifier fails the run closed rather than being returned.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

One graph, three modes, exposed as separate routes plus a single-endpoint envelope:

| Endpoint | Mode | Purpose |
|---|---|---|
| `POST /preflight` | `preflight` | Compare one shipment packet against its L/C terms |
| `POST /ingest` | `ingest` | Load document-requirement rules and prior bank-return history |
| `POST /feedback` | `feedback` | Record an operator's approved review points and promote them to the knowledge base |
| `POST /invoke` | any | Platform contract: `input` is a JSON string `{"mode", "payload", "case_ref"?}` |
| `GET /health` | — | Liveness |

**All three modes are write operations** — `preflight` inserts a case header for audit — so every
one of them requires a verified middleware identity. This is classified by side effect, not by the
route's name, and it means the standalone bearer-token path cannot reach any of them.

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation injected through the graph configuration. The Marketplace entry
  adapter in `cli.py` builds an Azure OpenAI client lazily from deployment secrets, validating that
  the configured endpoint is a bare resource endpoint before any client is constructed;
- authentication middleware that stamps a verified trust level, a `caller_id` and the `tenant_id`
  scope axis. Scope is never read from the request body;
- durable storage behind the service seams for the four tables the design defines
  (`shipment_packets`, `lc_terms_registry`, `bank_return_history`, `feedback_audit_events`);
- an approver allowlist for knowledge-base promotion; with none configured, no caller can promote
  (fail closed).

Runtime parameters live in `config/config.yaml`: `max_retry` (`3`), `timeout_s` (`60`),
`memory_enabled` (`false`), `hitl.enabled` (`false`), `llm.temperature` (`0.1`) and
`llm.max_tokens` (`2500`), and `retrieval.top_k` (`5`).

The client extracts document text; the template does not accept PDF or image bytes, does not
perform OCR, does not verify document authenticity, does not decide whether a presentation may be
accepted or paid, and does not give legal or financial advice.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform; deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The standalone server in `src/api/server.py` starts without the platform and `/health` answers
`{"status": "ok"}` — but nothing else is reachable, by design. Because all three modes are
write-classified, they require a verified middleware identity: a request with no identity is
**401** (wrong bearer token) or **403**, and even a valid `INVOKE_AUTH_TOKEN` bearer with the
STG scope defaults set is refused with *"Verified middleware identity is required."* The agent
would rather refuse than write an audit record for a caller it cannot name. The test suite runs
without a platform connection, a model, a database or network access.

## Input

`POST /preflight` takes `payload` (the case metadata plus the extracted L/C and document text as
JSON), an optional `case_ref` and an optional `session_id`. The server issues the `case_ref` for a
new case; a client-supplied one is ignored on this route, so a caller cannot attach a new lifecycle
to someone else's existing case reference. Raw extracted text is put into a scope- and
session-bound payload store immediately on receipt, and only the reference reaches the graph.

`POST /ingest` takes normalised JSON rows (document-requirement rules, prior bank returns); rows
containing credential patterns are rejected without echoing the offending value. `POST /feedback`
takes `case_ref` plus the approved review points; the approver recorded is the verified caller
identity, never a self-reported name in the payload, and a feedback call against a non-existent
case comes back as `status: error`. Re-sending the same event is idempotent.

```bash
curl -X POST http://127.0.0.1:8000/preflight \
  -H 'Content-Type: application/json' \
  -d '{
    "payload": {
      "lc_terms": {"amount": "100000.00", "currency": "USD", "expiry_date": "2026-10-31"},
      "documents": [
        {"doc_type": "commercial_invoice", "fields": {"amount": "100000.00"}},
        {"doc_type": "bill_of_lading", "fields": {"on_board_date": "2026-10-01"}}
      ]
    },
    "session_id": "example-session"
  }'
```

The example assumes authentication middleware in front of the server; without it the request is
rejected before the graph runs.

## Output

The standard AgentCore envelope is preserved. For `preflight`, `output` is a fixed six-field body:

| Field | Content |
|---|---|
| `facts` | Stage 1 deterministic findings — document presence, numeric comparison, deadline |
| `inconsistency_candidates` | Candidate inconsistencies — candidates, never assertions |
| `bank_inquiry_points` | Points that may warrant a query to the bank, including recalled prior returns |
| `correction_suggestions` | Candidate corrections |
| `evidence_map` | Claim-to-source map (L/C clause ID, document link ID) for every item above |
| `requires_human_review` | `{"flag": bool, "reasons": [str]}` |

`requires_human_review` is raised both when the output gate finds an assertive phrasing, a missing
citation or low confidence, and when Stage 1 itself found something — a missing required document,
a field outside tolerance, or a presentation deadline that is `late` or `at_risk`.

Each candidate is a closed, typed record (`claim_type`, `target`, `description`,
`cited_source_ids`); an undeclared key, a missing field, a wrong type or an unknown enum value
fails the run closed rather than being passed through.

## Security and Limitations

Every mode requires a verified middleware identity, and the tenant scope is enforced on both
retrieval and citation: a claim citing another tenant's source fails closed. Raw extracted document
text is vaulted outside graph state with a single-use, TTL-bound reference, so it does not reach a
checkpoint. Free text entering the knowledge base is masked **at write time** — masking only on
read would leave the original in the store, where one database dump, one checkpoint or one
overlooked read path exposes every row ever ingested. Knowledge-base promotion through `/feedback`
requires the verified caller to be in the approver allowlist; with no allowlist configured nobody
can promote.

Known limits, recorded rather than hidden:

- the framework's own output gate scans only top-level strings of a result; nested structures such
  as the preflight brief are not covered, which is exactly why the knowledge-base text gate is
  applied on the way in as well as on the way out;
- the client is responsible for extracting document text — no PDF, image or OCR path exists in
  this release, so extraction errors upstream are invisible to the agent;
- deadline evaluation falls back to the bill-of-lading issue date when no on-board date is present,
  and records which basis it used;
- storage seams ship as in-memory implementations; nothing survives a restart until a deployment
  supplies durable ones;
- the semantic comparison is model-generated and assistive. It does not replace the bank's document
  examination, and the operator remains the decision-maker.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), inner workflow subgraph
- Generation mode: `llm` (semantic comparison only; facts, deadlines and tolerances are deterministic)
- HITL: not enabled — operator review is recorded through `/feedback`
- Persistence: none in-process — no checkpointer; durable state lives in the injected stores
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using injected fakes and in-memory stores. Running the
agent itself needs the platform, authentication middleware, an injected `BaseLLM`, and durable
storage.

## Documentation

`docs/02_design.md` carries the mode design, the state schema, the deterministic Stage 1 rules,
the output contract and its acceptance matrix, the database schema and the raw-document handling
contract. `docs/03_test_spec.md` records the test specification and its results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
