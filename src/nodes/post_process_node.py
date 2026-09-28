"""Mode-aware output envelope + S-3 assertion-lexicon gate.

Design §2-6 / §9-2:
  - preflight → 6-field envelope (facts / inconsistency_candidates /
    bank_inquiry_points / correction_suggestions / evidence_map /
    requires_human_review).
  - Structural blocking (S3-1): payment/presentation/authenticity verdict
    fields must never appear.
  - Body-level gate (S3-2): scan ALL user-visible free-text for assertion
    lexicon (presentation / payment / authenticity verdicts). On a hit, redact
    the offending span and demote to requires_human_review (do not block).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.output_envelope.envelope_node import OutputEnvelopeNode

# ── S3-1: fields that must NEVER appear (structural verdict block) ────────────
# Keys are normalized (NFKC + casefold + non-alphanumeric stripped) before
# matching, so camelCase / full-width / separator variants cannot slip a verdict
# field through (review #3). Both an exact set and suffix/keyword patterns apply.
_PROHIBITED_VERDICT_KEYS = frozenset(
    {
        "paymentverdict",
        "presentationapproved",
        "presentationverdict",
        "discrepancyfinal",
        "discrepancyverdict",
        "authenticityverdict",
        "isauthentic",
        "isgenuine",
        "accept",
        "acceptance",
        "accepted",
        "honored",
        "honoured",
    }
)
# keyword/suffix patterns applied to the ASCII-normalized key form. The
# preflight envelope carries NO legitimate accept/verdict/authenticity key, so
# any key containing these tokens is a bank-decision leak and is refused.
_PROHIBITED_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"verdict"),
    re.compile(r"approv"),
    re.compile(r"authentic"),
    re.compile(r"accept"),  # accept / accepted / acceptance / isAccepted / paymentAccepted
    re.compile(r"honou?r"),
    re.compile(r"discrepancyfinal"),
)
# Non-ASCII (Japanese) verdict-key tokens, matched on the NFKC form before the
# ASCII strip (which would collapse Japanese keys to "" and let them through —
# review final #1). Presentation/payment acceptance, discrepancy verdict, and
# authenticity keys.
_PROHIBITED_KEY_JA: tuple[str, ...] = (
    "支払可否",
    "支払判定",
    "提示承認",
    "提示可否",
    "受理",
    "受諾",
    "真正性",
    "真正判定",
    "真贋",
    "ディスクレ判定",
)


def _normalize_key(key: Any) -> str:
    """NFKC + casefold + strip non-alphanumerics so camelCase / full-width /
    separator-variant verdict keys all collapse to one comparable form."""
    text = unicodedata.normalize("NFKC", str(key)).casefold()
    return re.sub(r"[^0-9a-z]", "", text)


def _is_prohibited_key(key: Any) -> bool:
    # Japanese/non-ASCII verdict tokens: match on the NFKC form BEFORE the ASCII
    # strip collapses them to "" (review final #1).
    nfkc = unicodedata.normalize("NFKC", str(key)).casefold()
    if any(token in nfkc for token in _PROHIBITED_KEY_JA):
        return True
    norm = _normalize_key(key)
    if norm in _PROHIBITED_VERDICT_KEYS:
        return True
    return any(pattern.search(norm) for pattern in _PROHIBITED_KEY_PATTERNS)

# ── S3-2: assertion lexicon (NFKC + casefold before matching) ────────────────
# Product-safety mitigation, NOT a compliance guarantee (design §2-6 note /
# reference_s3_lexicon_is_product_safety_not_legal_compliance). Owner = business.
_ASSERTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # presentation / payment acceptance verdicts (EN + JA)
    re.compile(r"銀行は?.{0,8}(受理|受領|受諾|引受)(すべき|する|できる|しなければ)"),
    re.compile(r"(提示|呈示|支払|決済)(は|が)?.{0,6}(可能|認められる|受理される|完了する)"),
    re.compile(r"(discrepanc(y|ies)\s+(are|is)\s+(waived|cured|resolved))"),
    re.compile(r"the\s+bank\s+(must|shall|should|will)\s+(accept|honou?r|pay|present)"),
    re.compile(r"(documents?\s+(are|is)\s+(compliant|acceptable|in\s+order|honou?red))"),
    re.compile(r"(payment\s+(is|will\s+be)\s+(guaranteed|assured|certain))"),
    # authenticity verdicts (EN + JA)
    re.compile(r"(書類|文書)(は|が)?.{0,6}(真正|本物|正当|有効)である"),
    re.compile(r"(documents?\s+(are|is)\s+(genuine|authentic|valid|verified))"),
)

# Control keys never scanned/redacted (structural, not user-authored prose).
_CONTROL_KEYS = frozenset({"mode", "gate_kind", "status", "requires_human_review"})

_REDACTED = "[redacted: verdict phrasing — see requires_human_review]"


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # FIN modes: preflight (not "invoke"), ingest, feedback.
    allowed_modes = ("preflight", "ingest", "feedback")
    gate_kind_by_mode = {"preflight": "draft", "ingest": "ingest", "feedback": "feedback"}
    required_fields_by_mode = {
        "preflight": (
            "mode",
            "gate_kind",
            "facts",
            "inconsistency_candidates",
            "bank_inquiry_points",
            "correction_suggestions",
            "evidence_map",
            "requires_human_review",
        ),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_result"),
    }
    max_field_chars = None

    def execute(self, state: dict) -> dict:
        if all(key in state for key in ("correlation_id", "session_id", "thread_id", "trace_id")):
            InvocationContext.from_state(state)
        if state.get("status") == AgentStatus.ERROR.value:
            formatted = {
                "mode": state.get("mode", ""),
                "gate_kind": "error",
                "status": AgentStatus.ERROR.value,
                "error_log": state.get("error_log", []),
            }
            emit_trace_event("lc_preflight_output_error", {}, state)
            return {"formatted_output": formatted, "status": AgentStatus.ERROR.value}

        mode = state.get("mode", "")
        if mode not in self.allowed_modes:
            emit_trace_event("lc_preflight_output_error", {}, state)
            return {
                "formatted_output": {"mode": "", "gate_kind": "error", "status": AgentStatus.ERROR.value, "error_log": ["mode_missing"]},
                "status": AgentStatus.ERROR.value,
            }

        # Dispatch by FIN modes (base only knows invoke/ingest/feedback).
        if mode == "preflight":
            formatted = self.format_preflight(state)
        elif mode == "ingest":
            formatted = self.format_ingest(state)
        else:  # feedback
            formatted = self.format_feedback(state)

        if not isinstance(formatted, dict):
            raise SecurityViolationError("PostProcessNode: formatter returned non-dict formatted_output")
        formatted["mode"] = mode
        formatted["gate_kind"] = self.gate_kind_by_mode[mode]
        self._fold_optional_state_fields(state, formatted)
        emit_trace_event("lc_preflight_output_envelope", {"mode": mode, "gate_kind": formatted.get("gate_kind")}, state)

        result: dict[str, Any] = {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
            "_output_envelope_hook_state": state,
        }
        if state.get("degradation_reason"):
            result["degradation_reason"] = state["degradation_reason"]
        return result

    # ── preflight envelope (6 fields) ────────────────────────────────────
    def format_preflight(self, state: dict) -> dict[str, Any]:
        brief = state.get("preflight_brief") if isinstance(state.get("preflight_brief"), dict) else {}
        rhr = state.get("requires_human_review")
        if not isinstance(rhr, dict):
            rhr = {"flag": False, "reasons": []}
        return {
            "facts": brief.get("facts", {}),
            "inconsistency_candidates": brief.get("inconsistency_candidates", []),
            "bank_inquiry_points": brief.get("bank_inquiry_points", []),
            "correction_suggestions": brief.get("correction_suggestions", []),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": rhr,
        }

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_result": state.get("feedback_result", {}) or {}}

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        # Run the S-3 verdict-key block and assertion scan BEFORE the base
        # sanitizer's depth-8 truncation. Otherwise a deep-nested verdict key or
        # assertion string would be collapsed to "[truncated…]" and escape the
        # scan without being flagged (review round-2 #S3-2).
        self._assert_no_verdict_keys(formatted)
        if formatted.get("mode") == "preflight":
            reasons: list[str] = []
            formatted = self._scan_and_redact(formatted, reasons)
            if reasons:
                rhr = formatted.get("requires_human_review")
                if not isinstance(rhr, dict):
                    rhr = {"flag": False, "reasons": []}
                existing = rhr.get("reasons") if isinstance(rhr.get("reasons"), list) else []
                rhr = {"flag": True, "reasons": sorted(set(existing) | set(reasons))}
                formatted["requires_human_review"] = rhr
        # base structural sanitizer last (depth guard / optional truncation).
        return super()._sanitize_client_echoes(formatted)

    def _assert_no_verdict_keys(self, node: Any, depth: int = 0) -> None:
        if depth >= 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if _is_prohibited_key(key):
                    raise SecurityViolationError(f"S-3 gate: prohibited verdict field {key!r} in output")
                self._assert_no_verdict_keys(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                self._assert_no_verdict_keys(item, depth + 1)

    def _scan_and_redact(self, node: Any, reasons: list[str], key_hint: str = "", depth: int = 0) -> Any:
        """Recursively scan EVERY user-visible string in the preflight envelope
        for assertion phrasing — not a fixed list of paths (review #2). Any LLM
        free-text key (description / rationale / text / note / …) at ANY depth is
        covered. The control-key skip applies ONLY at the root level (depth 0);
        a nested key named e.g. `requires_human_review` inside an LLM item is a
        smuggling vector and is scanned (review round-2 #S3-2). Depth-limit hits
        fail closed (redact) rather than passing text through."""
        if depth >= 20:
            # pathological nesting: refuse to emit unscanned user-visible text.
            if isinstance(node, str):
                reasons.append("assertion_lexicon:depth_limit")
                return _REDACTED
            return node
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if depth == 0 and key in _CONTROL_KEYS:
                    out[key] = value
                    continue
                out[key] = self._scan_and_redact(value, reasons, key_hint=str(key), depth=depth + 1)
            return out
        if isinstance(node, list):
            return [self._scan_and_redact(item, reasons, key_hint=key_hint, depth=depth + 1) for item in node]
        if isinstance(node, str) and _has_assertion(node):
            reasons.append(f"assertion_lexicon:{key_hint or 'text'}")
            return _REDACTED
        return node


def _has_assertion(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return any(pattern.search(normalized) for pattern in _ASSERTION_PATTERNS)
