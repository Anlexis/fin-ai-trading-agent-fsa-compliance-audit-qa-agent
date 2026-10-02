"""AgentCore Platform v1.0"""

# FIN-C2-196 - PreProcessNode (outer pre_process slot; caller-input boundary)
#
# Node contract: extend FunctionNode; implement execute(self, state) -> dict -
# no `config` parameter. Return ONLY the fields this node changes (never full
# state). Read input_context via _without_platform_context(state.get("input_context", {})) - read-only.
# Never import from mediator/, api/, or other agents.
#
# This node is the template's caller-input boundary and owns three controls,
# in this order:
#
#   1. Trust gate. The manifest declares required_trust_level
#      VERIFIED_EXTERNAL, so this node gates external callers before the inner
#      domain workflow runs.
#
#   2. Prompt-injection screen, applied by THIS node rather than delegated to
#      the surrounding framework. A template that relies on the framework gate
#      alone is fail-open wherever that gate is absent or configured off: the
#      payload reaches the answer path and the run returns success. The screen
#      runs on the free-text question AND on the structured caller context,
#      depth-first including field names, and it runs BOTH before and after the
#      identifier redaction below - a sanitizer is not a refusal, and stripping
#      content can turn a detectable token attack into undetectable prose or
#      re-assemble a directive that was spliced with markup.
#
#   3. Identifier redaction. Direct identifiers (account numbers, IBANs,
#      e-mail) are surface-stripped from the free-text question AND from the
#      free-text fields of the structured caller context, so raw identifiers
#      never reach the inner domain nodes or the checkpoint store. Redacting
#      only the question would leave the structured channel as an open path
#      for the same data.
#
# Rejection is fail-closed and names the finding class only - never the
# offending text and never an unrecognised caller field name verbatim.

import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.caller_input import mask_field_name
from src.schemas.injection_screen import screen_payload


# The Marketplace runner seeds input_context with its own conversation history on every
# invocation (shared/bootstrap/marketplace_app.py); the caller neither sends that key nor can
# suppress it, and the build_input_context hook can only overwrite its value, never remove it.
# It is platform plumbing rather than caller data, so it is dropped here, before the caller
# contract runs: the unknown-field guard below stays strict for everything a caller can
# actually send, and no value screen is ever asked to judge a transcript that contains this
# agent's own earlier answers. The value may also be None, which this tolerates.
_PLATFORM_CONTEXT_KEYS = frozenset({"conversation_history"})


def _without_platform_context(raw: Any) -> Any:
    """The caller-supplied half of input_context, platform-injected keys removed."""
    if not isinstance(raw, dict):
        return raw
    return {k: v for k, v in raw.items() if k not in _PLATFORM_CONTEXT_KEYS}


# Surface-level identifier patterns redacted before validated_input is
# written. Downstream domain nodes only ever operate on the normalised
# question text and knowledge-base passage summaries, never raw account or
# employee identifiers a compliance officer might paste in while describing a
# case.
_PII_PATTERNS: List[re.Pattern[str]] = [
    # IBAN: 2 letters + 2 digits + up to 30 alphanumerics.
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    # Long bank / account numbers: 10-19 consecutive digits (optionally grouped).
    re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{2,11}\b"),
    # E-mail addresses.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
]
_PII_REPLACEMENT = "[REDACTED]"

# Hard cap on the free-text question accepted at the boundary. The structured
# channel has its own per-field caps in the intake node.
_MAX_INPUT_CHARS = 8000

# Caller-context fields this template understands. Anything else is reported as
# a masked name; the value is never read and never echoed.
_KNOWN_CONTEXT_FIELDS = frozenset({"channel", "topic", "top_k", "score_threshold", "case_ref"})

# Caller-context fields that carry free text and therefore get the same
# identifier redaction as the question itself.
_FREE_TEXT_CONTEXT_FIELDS = ("channel",)


def _surface_strip_identifiers(text: str) -> str:
    """Redact obvious direct-identifier tokens from a free-text string."""
    for pattern in _PII_PATTERNS:
        text = pattern.sub(_PII_REPLACEMENT, text)
    return text


def _redact_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Apply identifier redaction to the free-text fields of the caller context."""
    redacted = dict(context)
    for field in _FREE_TEXT_CONTEXT_FIELDS:
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _surface_strip_identifiers(value)
    return redacted


class PreProcessNode(FunctionNode):
    """Validate, screen and redact the caller payload before the domain workflow.

    Rejects empty or non-string questions, refuses prompt-injection payloads on
    either caller channel, and surface-strips direct account / employee
    identifiers from both.
    """

    # Explicit by design, not inherited implicitly. Outer backbone gate slot -
    # matches the manifest's declared required_trust_level (config/agent.yaml).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: AgentState, reason: str, message: str) -> Dict[str, Any]:
        """Fail-closed rejection: audit the reason code, publish nothing."""
        emit_trace_event("pre_process_rejected", {"reason": reason}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {message}"],
        }

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        raw_context = _without_platform_context(state.get("input_context", {}))  # read-only
        context: Dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            return self._reject(state, "empty_input", "user_input is empty or missing")
        if len(user_input) > _MAX_INPUT_CHARS:
            return self._reject(state, "input_too_large", "user_input exceeds the permitted length")

        # Unrecognised caller-context fields are surfaced by MASKED name. The
        # value behind an unknown name is never read, so it cannot reach the
        # pipeline, and the name itself is never echoed verbatim.
        unknown = [
            mask_field_name(key) for key in context if not (isinstance(key, str) and key in _KNOWN_CONTEXT_FIELDS)
        ]
        if unknown:
            return self._reject(
                state,
                "unknown_context_field",
                f"unrecognised caller context field(s): {', '.join(sorted(unknown))}",
            )

        # Screen 1 - RAW. Control tokens are caught here, before any strip can
        # silently remove them and forward the surrounding directive as prose.
        finding = screen_payload(user_input) or screen_payload(context)
        if finding:
            return self._reject(
                state,
                f"injection_{finding}",
                f"request rejected by the input screen (finding class: {finding})",
            )

        validated_input = _surface_strip_identifiers(user_input.strip())
        redacted_context = _redact_context(context)

        # Screen 2 - POST-SANITIZE. Redaction rewrites the text, which can
        # re-assemble a directive that was spliced around an identifier. The
        # screen is cheap; running it twice closes that gap.
        finding = screen_payload(validated_input) or screen_payload(redacted_context)
        if finding:
            return self._reject(
                state,
                f"injection_{finding}_post_sanitize",
                f"request rejected by the input screen after redaction (finding class: {finding})",
            )

        # A compliance question was accepted, screened and surface-redacted.
        # The payload carries sizes and a channel label, never caller content.
        emit_trace_event(
            "pre_process_complete",
            {
                "input_chars": len(validated_input),
                "context_fields": len(redacted_context),
            },
            state,
        )

        channel = redacted_context.get("channel")
        return {
            "validated_input": validated_input,
            "enriched_context": {
                "source": "FSATradingComplianceAuditAgent",
                "channel": channel if isinstance(channel, str) else "unknown",
            },
            "status": AgentStatus.SUCCESS.value,
        }
