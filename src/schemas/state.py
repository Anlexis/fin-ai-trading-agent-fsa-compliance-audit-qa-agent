"""AgentCore Platform v1.0"""

# State must be a flat TypedDict - never a Pydantic model. Checkpoints are
# serialized with msgpack, and Pydantic objects corrupt silently there.
# Extend AgentState with agent-specific fields only.  Do NOT add credentials,
# secrets, or Pydantic models.
#
# Checkpoint safety: structured fields (dict / list[dict]) are stored as JSON
# STRINGS, not bare Python containers - a bare dict/list in a checkpointed
# State field does not survive the round trip. Producers serialize with
# to_json() on write; consumers deserialize with from_json() on read.
#
# FIN-C2-196 - FSA Trading Compliance Audit Agent (Cat 2 RAG, nested).
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Confidentiality note: direct identifiers (account numbers, IBAN, e-mail)
# in the question payload are surface-stripped by PreProcessNode before any
# field is written to State.  Only the normalised
# compliance question, KB passage summaries, and the final grounded analysis
# are persisted - never raw customer/employee identifiers.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input -> the supplied ``default`` so a missing or
    corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for FIN-C2-196.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    Domain fields are NotRequired so the TypedDict is valid at graph
    initialisation, before any node has written a value.
    """

    # ------------------------------------------------------------------
    # Outer layer - set by PreProcessNode / ComplianceAuditGraphNode.merge_output
    # ------------------------------------------------------------------

    # Identifier-stripped, screened question payload produced by PreProcessNode.
    # Raw input is NOT persisted beyond PreProcessNode.
    validated_input: NotRequired[str]

    # Final compliance-audit answer, mapped from the inner graph's
    # formatted_answer output via merge_output.
    compliance_answer: NotRequired[str]

    # Resolved FSA topic slug - one of "definitive_judgment",
    # "mrm_documentation", "incident_notification", "human_oversight", or
    # None when the question matched no topic keywords. Set by the inner
    # InputValidateNode and re-surfaced at the outer layer by merge_output()
    # (outer copy) so PostProcessNode / get_output() can read it without
    # reaching into the inner graph.
    classification_topic: NotRequired[Optional[str]]

    # JSON STRING (to_json) of the topic-specific documentation/remediation
    # checklist. Deserialised shape: list[str]. Set by GenerateAnswerNode,
    # re-surfaced at the outer layer by merge_output().
    remediation_checklist: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer - domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # InputValidateNode outputs
    # Normalised free-text compliance question (whitespace-collapsed, length-capped).
    search_query: NotRequired[str]

    # JSON STRING (to_json) of parsed structured query params. Deserialised
    # dict shape: {"topic": str | None, "top_k": int | None}.
    # Consumers (RetrieveNode, RerankFilterNode) read it back via from_json().
    query_filters: NotRequired[Optional[str]]

    # Runtime `retrieval` block forwarded by ComplianceAuditGraphNode.
    # _parent_config() -> DomainWorkflowGraph._extra_initial_state().
    # JSON STRING (to_json) of {"top_k": int, "score_threshold": float,
    # "kb_path": str}. Consumers (RetrieveNode, RerankFilterNode) read it
    # back via from_json().
    retrieval_config: NotRequired[Optional[str]]

    # UNVALIDATED structured caller context, carried across the outer/inner
    # graph boundary by the context bridge and seeded here by
    # DomainWorkflowGraph._extra_initial_state(). JSON STRING (to_json) of the
    # raw caller mapping. Read ONLY by InputValidateNode, which validates it;
    # no other node may consume this field.
    caller_context: NotRequired[Optional[str]]

    # Set by InputValidateNode when a caller field failed validation. The inner
    # graph's route_after_intake() reads it and ends the run, so a rejected
    # request never reaches retrieval or answer assembly.
    intake_rejected: NotRequired[bool]

    # Caller-supplied audit reference, validated to an inert identifier
    # ([a-z0-9_], 1-32 chars) before it is rendered into the report. Free text
    # in a slot that reaches the output is caller-controlled output injection,
    # so anything outside that alphabet is rejected rather than escaped.
    case_ref: NotRequired[Optional[str]]

    # RetrieveNode output
    # JSON STRING (to_json) of scored KB candidates. Deserialised shape:
    # list[dict], each entry {"id": str, "title": str, "category": str,
    # "source": str, "score": float, "excerpt": str}.
    # Consumers (RerankFilterNode) read it back via from_json().
    retrieved_passages: NotRequired[Optional[str]]

    # RerankFilterNode output
    # JSON STRING (to_json) of reranked + threshold-filtered passages, capped
    # at top_k. Same entry shape as retrieved_passages.
    # Consumers (GenerateAnswerNode) read it back via from_json().
    ranked_passages: NotRequired[Optional[str]]

    # GenerateAnswerNode output
    # Rule-assembled grounded compliance analysis body with numbered citation
    # markers (does NOT include the checklist or the disclaimer - those are
    # composed by OutputFormatNode).
    compliance_analysis: NotRequired[str]

    # The caller's question, carried separately from compliance_analysis so
    # OutputFormatNode can render it as quoted material and exclude it from the
    # definitive-verdict control, which polices what the AGENT asserts.
    audited_question: NotRequired[str]

    # JSON STRING (to_json) of citations. Deserialised shape: list[dict],
    # each entry {"ref": int, "id": str, "title": str, "source": str}.
    # Consumers (OutputFormatNode) read it back via from_json(). Also
    # re-surfaced at the outer layer by merge_output().
    citations: NotRequired[Optional[str]]

    # OutputFormatNode output
    # Final formatted answer (classification line + analysis body + sources +
    # checklist + disclaimer, after the block-definitive-classification
    # scan). Written by OutputFormatNode; surfaced to the outer graph via
    # get_output() -> merge_output().
    formatted_answer: NotRequired[str]

    # Validation / parse notes accumulated during intake (no PII).
    # JSON STRING (to_json) of list[str].
    intake_notes: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit - framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
