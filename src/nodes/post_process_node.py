"""AgentCore Platform v1.0"""

# FIN-C2-196 - PostProcessNode (outer post_process slot; credential output gate)
#
# This node calls the MODULE-LEVEL `_security_gate_output()` scan from
# execute() itself. The output is scanned for disallowed content (API keys,
# JWT tokens, Bearer tokens, raw credential assignments) — generic,
# domain-agnostic patterns.
#
# The scan walks nested dict/list structures RECURSIVELY rather than
# top-level strings only. A credential-shaped value nested one level inside
# a returned payload dict is the ordinary case, not an exotic one, and a
# top-level-only scan reports zero findings on it.
#
# On a violation, formatted_output is replaced with a sanitised stub and an
# error status is returned. The scan is unconditional: every execute() path
# runs it before any value can be returned, and no flag, argument or state
# field can suppress it. No _extra_security_gate_input/_output instance
# methods are defined on this node — the framework auto-wraps such hooks.
#
# This module-level `_security_gate_output()` is also imported and reused by
# `FSATradingComplianceAuditAgent.get_output()` (src/graph/graph.py) before it
# surfaces any structured field (classification_topic /
# remediation_checklist / citations) - the same gate, applied a second time
# at the outer envelope boundary, fail-closed.
#
# This is an outer backbone gate slot - the manifest declares
# required_trust_level: "VERIFIED_EXTERNAL" (config/agent.yaml).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_factory import resolve_llm
from src.services.llm_review import render_review, review_result

logger = logging.getLogger(__name__)

# Disallowed content patterns. Each tuple: (name, compiled regex) —
# order matters (most specific first).
_DISALLOWED_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    # API key patterns: sk-..., pk-..., ak-...
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # JWT: three base64url segments separated by dots
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Bearer token in Authorization-like context
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE)),
    # Credential assignment patterns
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
]

_SANITISED_STUB = (
    "[OUTPUT BLOCKED by the credential gate - disallowed content detected. "
    "Review the generated output and retry without credential-like strings.]"
)


def _security_gate_output(content: Any) -> Optional[str]:
    """Run the output content gate, recursively over nested structures.

    Accepts a plain string, or a dict/list/tuple/set that may nest strings
    at any depth. Call sites pass vetted scalar fields, but the scan itself
    must not be top-level-string-only, or a credential-shaped value nested
    one level down slips through unscanned.

    Returns the name of the first matched violation, or None if clean.
    """
    if isinstance(content, str):
        for name, pattern in _DISALLOWED_PATTERNS:
            if pattern.search(content):
                return str(name)
        return None
    if isinstance(content, dict):
        for value in content.values():
            violation = _security_gate_output(value)
            if violation:
                return violation
        return None
    if isinstance(content, (list, tuple, set)):
        for value in content:
            violation = _security_gate_output(value)
            if violation:
                return violation
        return None
    return None  # non-string scalar (int/float/bool/None) - nothing to scan


class PostProcessNode(FunctionNode):
    """Output gate: scan the final compliance answer for disallowed content.

    Outer backbone post_process slot. Reads state["result"] (the merged
    formatted_answer from ComplianceAuditGraphNode.merge_output()) and
    applies the content-safety gate before the response is returned to
    the caller.

    Input state keys:
        result: final formatted compliance answer (from merge_output)

    Output state keys (partial dict):
        formatted_output: sanitised output (unchanged answer if clean;
                          blocked stub on violation)
        result:           gated alongside formatted_output (blocked stub on
                          violation; unchanged on the clean/empty paths)
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
                          (plain strings — never the bare enum in State)
        error_log:        (on error) list of error messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        result = state.get("result") or ""
        scanned = "" if result is None else str(result)

        # Credential gate — unconditional. Every path through execute() reaches this
        # scan before a value can be returned; there is no suppression flag.
        _llm, _ = resolve_llm(None, state)
        _remarks = review_result(
            _llm,
            user_input=str(state.get("user_input") or ""),
            result=scanned,
            domain="FIN FSATradingComplianceAuditAgent",
        )
        _review = render_review(_remarks)
        # Remarks are LLM text derived from the caller's raw words, so they pass through the
        # same gate the answer does -- appending after the gate would put unscanned text past
        # it. A tripped review is dropped on its own: withholding a correct answer because an
        # advisory remark quoted an identifier would let the review change the outcome, and
        # the whole design rests on it being unable to.
        if _review and isinstance(scanned, str) and not _security_gate_output(scanned + _review):
            scanned = scanned + _review

        violation = _security_gate_output(scanned)
        if violation:
            logger.error(
                "PostProcessNode: OUTPUT BLOCKED - violation type: %s",
                violation,
            )
            # Domain audit: an output was blocked. Payload carries the
            # violation type only — never the offending text (non-PII).
            emit_trace_event(
                "post_process_blocked",
                {"violation": violation},
                state,
            )
            return {
                "formatted_output": _SANITISED_STUB,
                "result": _SANITISED_STUB,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked - " f"disallowed content detected ({violation})"],
            }

        if not scanned.strip():
            # No answer was generated - forward as-is (non-fatal).
            return {
                "formatted_output": result,
                "status": AgentStatus.SUCCESS.value,
            }

        # Clean — domain audit: record that a finalized answer was emitted.
        emit_trace_event(
            "post_process_complete",
            {"output_chars": len(scanned)},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
