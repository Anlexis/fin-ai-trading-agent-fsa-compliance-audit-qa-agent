"""AgentCore Platform v1.0"""

# FIN-C2-196 - InputValidateNode
# Domain node 1 and the intake boundary: parse, VALIDATE, normalise and
# CLASSIFY the incoming compliance question.
#
# Two caller channels arrive here:
#
#   1. The free-text question, passed into the inner graph as its user_input
#      by the outer GraphNode (already screened and identifier-stripped by
#      PreProcessNode). A JSON envelope in that string is still accepted for
#      callers that used the older single-channel form.
#
#   2. The structured caller context (`caller_context`), carried across the
#      graph boundary by the context bridge. THIS node is the only consumer:
#      every field is validated against explicit bounds before any other node
#      can see it.
#
# Validation is FAIL-CLOSED. A caller-supplied number that is not finite and
# in range, or a caller-supplied string outside its permitted set, ends the
# run with an error that names the FIELD and never the value. Silently
# clamping a bad value instead would hand the caller a result computed from
# parameters they did not ask for, and a non-finite threshold that slipped
# through would disable the relevance filter entirely, because every
# comparison against NaN is false.
#
# Absent context is not an error: with no structured fields the run degrades
# to the configured defaults and answers the free-text question.
#
# Classification scores the question against a fixed keyword table for the
# four regulatory topics (see docs/02_design.md). A caller-supplied `topic`
# is honoured only when it names one of the four valid slugs. This is not a
# model-based classifier - it is a transparent, testable keyword score.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import json
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.caller_input import (
    CallerInputError,
    finite_in_range,
    inert_identifier,
    one_of,
)
from src.schemas.state import from_json, to_json

# Hard cap on the normalised query length (defence-in-depth on input size).
_MAX_QUERY_CHARS = 2000

# Explicit bounds for every caller-supplied number. These are the ONLY values
# a caller may steer; anything outside the stated range is refused, not clamped.
_TOP_K_MIN = 1
_TOP_K_MAX = 20
_SCORE_THRESHOLD_MIN = 0.0
_SCORE_THRESHOLD_MAX = 1.0

_WHITESPACE_RE = re.compile(r"\s+")

# The four fixed FSA regulatory topics (docs/02_design.md). Order also acts
# as the tie-break priority when two topics score equally.
TOPIC_ORDER: List[str] = [
    "definitive_judgment",
    "mrm_documentation",
    "incident_notification",
    "human_oversight",
]

# Deterministic keyword table - lowercase substring phrases scored against
# the lowered question text. Kept intentionally simple/transparent (no NLP
# deps) so classification is reproducible and unit-testable.
_TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "definitive_judgment": [
        "definitive",
        "assertive",
        "guarantee",
        "guaranteed",
        "certain profit",
        "certain to",
        "unconditional",
        "38-2",
        "article 38",
        "misleading",
        "conclusive",
        "断定",
    ],
    "mrm_documentation": [
        "mrm",
        "model risk",
        "model validation",
        "model inventory",
        "model card",
        "documentation",
        "model governance",
        "model review",
        "model owner",
    ],
    "incident_notification": [
        "incident",
        "notification",
        "48-hour",
        "48 hour",
        "breach",
        "outage",
        "notify",
        "reportable",
        "report the incident",
    ],
    "human_oversight": [
        "human oversight",
        "human-in-the-loop",
        "human in the loop",
        "review",
        "approval",
        "sign-off",
        "signoff",
        "override",
        "escalation",
        "reviewer",
    ],
}


def classify_topic(text: str) -> Optional[str]:
    """Deterministic keyword classification into one of the four FSA topics.

    Returns the highest-scoring topic slug, ties broken by TOPIC_ORDER
    position, or None when no topic keyword matched at all.
    """
    lowered = text.lower()
    best_topic: Optional[str] = None
    best_score = 0
    for topic in TOPIC_ORDER:
        score = sum(1 for phrase in _TOPIC_KEYWORDS[topic] if phrase in lowered)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


def _validate_top_k(value: Any) -> Optional[int]:
    """Validate the caller top_k override: a whole number within the bounds.

    Raises CallerInputError (naming the field, never the value) rather than
    clamping. A clamped value means answering a question the caller did not
    ask; refusing says so.
    """
    if value is None:
        return None
    return int(
        finite_in_range(
            value,
            field="top_k",
            minimum=_TOP_K_MIN,
            maximum=_TOP_K_MAX,
            integer=True,
        )
    )


def _validate_score_threshold(value: Any) -> Optional[float]:
    """Validate the caller relevance-floor override.

    Finiteness is the point of this check, not a formality: NaN parses cleanly
    through float() and arrives intact through a JSON body, and every
    comparison against it is false - so a NaN threshold would silently drop
    every retrieved passage while the run reported success.
    """
    if value is None:
        return None
    return finite_in_range(
        value,
        field="score_threshold",
        minimum=_SCORE_THRESHOLD_MIN,
        maximum=_SCORE_THRESHOLD_MAX,
    )


def _validate_topic(value: Any) -> Optional[str]:
    """Validate the caller topic override against the four permitted slugs."""
    if value is None:
        return None
    return one_of(value, tuple(TOPIC_ORDER), field="topic")


def _validate_case_ref(value: Any) -> Optional[str]:
    """Validate the caller audit reference, which is rendered into the report.

    Locked to an inert identifier alphabet. This value reaches the output, and
    free text in an output slot is caller-controlled output injection.
    """
    if value is None:
        return None
    return inert_identifier(value, field="case_ref")


# Every caller-steerable field, in one place. The validators are looked up
# from this table rather than called ad hoc, so adding a field cannot
# accidentally add an unvalidated one.
_FIELD_VALIDATORS: Dict[str, Any] = {
    "topic": _validate_topic,
    "top_k": _validate_top_k,
    "score_threshold": _validate_score_threshold,
    "case_ref": _validate_case_ref,
}


def _collect_caller_fields(envelope: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the two caller channels into one mapping of raw field values.

    The structured context is authoritative where both channels name the same
    field, so a caller migrating from the JSON-envelope form to the structured
    channel gets the value they most recently supplied.
    """
    fields: Dict[str, Any] = {}
    for source in (envelope, context):
        if not isinstance(source, dict):
            continue
        for key in _FIELD_VALIDATORS:
            if key in source and source[key] is not None:
                fields[key] = source[key]
    return fields


class InputValidateNode(FunctionNode):
    """Validate the caller payload, then normalise and classify the question.

    Input state keys:
        validated_input | user_input: screened, identifier-stripped question
        caller_context:               JSON string of the raw structured context

    Output state keys (partial dict):
        search_query:         normalised free-text compliance question
        query_filters:        JSON dict {"topic": str|None, "top_k": int|None,
                              "score_threshold": float|None}
        classification_topic: resolved topic slug, or None (unclassified)
        case_ref:             validated inert audit reference, or None
        intake_notes:         (when anomalies were seen) JSON list[str]

    On a rejected caller field the node returns an error status, sets
    intake_rejected, and publishes NO query, filters or classification - the
    inner graph routes straight to END, so nothing downstream can act on a
    partially validated request.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _reject(self, state: AgentState, error: CallerInputError) -> Dict[str, Any]:
        """Fail-closed rejection naming the field, never the rejected value."""
        emit_trace_event(
            "input_validate_rejected",
            {"field": error.field, "reason": error.reason},
            state,
        )
        return {
            "intake_rejected": True,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"InputValidateNode: caller field '{error.field}' rejected " f"({error.reason})."],
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            "formatted_output": "Request could not be completed. "
            + (f"InputValidateNode: caller field '{error.field}' rejected ({error.reason})."),
        }

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")
        notes: List[str] = []

        query = ""
        envelope: Dict[str, Any] = {}

        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            payload: Any = None
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    notes.append(
                        "InputValidateNode: JSON-looking input did not parse - " "treated as plain text question."
                    )
            if isinstance(payload, dict):
                query = str(payload.get("query") or payload.get("question") or "")
                envelope = payload
            else:
                query = text
        else:
            notes.append("InputValidateNode: empty request - no question to audit.")

        context = from_json(state.get("caller_context"), {}) or {}
        if not isinstance(context, dict):
            context = {}

        # Validate every caller-steerable field. The first failure ends the
        # run; no partially validated request reaches the pipeline.
        supplied = _collect_caller_fields(envelope, context)
        validated: Dict[str, Any] = {}
        try:
            for field, validator in _FIELD_VALIDATORS.items():
                if field in supplied:
                    validated[field] = validator(supplied[field])
        except CallerInputError as error:
            return self._reject(state, error)

        # Normalise whitespace and cap length. Collapsing whitespace also means
        # the echoed question can never introduce a line break, so it cannot
        # forge a heading or a list item in the rendered report.
        query = _WHITESPACE_RE.sub(" ", query).strip()
        if len(query) > _MAX_QUERY_CHARS:
            query = query[:_MAX_QUERY_CHARS]
            notes.append(f"InputValidateNode: query truncated to {_MAX_QUERY_CHARS} chars.")

        classification_topic = validated.get("topic") or classify_topic(query)
        filters = {
            "topic": classification_topic,
            "top_k": validated.get("top_k"),
            "score_threshold": validated.get("score_threshold"),
        }

        # Question parsed, validated, normalised and classified. The payload
        # records shapes and counts, never caller content.
        emit_trace_event(
            "input_validate_complete",
            {
                "query_chars": len(query),
                "classification_topic": classification_topic or "unclassified",
                "caller_fields": sorted(validated),
            },
            state,
        )

        out: Dict[str, Any] = {
            "search_query": query,
            "query_filters": to_json(filters),
            "classification_topic": classification_topic,
            "case_ref": validated.get("case_ref"),
            "intake_rejected": False,
        }
        if notes:
            out["intake_notes"] = to_json(notes)
        return out
