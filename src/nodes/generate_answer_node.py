"""AgentCore Platform v1.0"""

# FIN-C2-196 - GenerateAnswerNode
# Domain node 4: assemble the grounded compliance analysis from the ranked
# KB passages, plus the topic-specific documentation/remediation checklist.
#
# The analysis is DETERMINISTIC - rule-assembled from the ranked passages
# only, with no model call. It is a hedged classification line (never an
# unconditional verdict - see the definitive-verdict control in
# OutputFormatNode) plus one cited point per passage, each carrying a numbered
# citation marker [n]. Nothing outside the ranked_passages input reaches the
# analysis body, so the output is grounded by construction. The checklist is
# derived from the FIXED per-topic table, independent of retrieval results, so
# a question always gets actionable next steps even when the knowledge base
# has no matching passage.
#
# THE CALLER'S QUESTION IS NOT PART OF THE ANALYSIS BODY. It is emitted as a
# separate field so the formatter can render it as quoted material. The
# definitive-verdict control polices what this agent ASSERTS; running it over
# text the caller wrote would suppress the answer whenever a compliance
# officer quotes the very phrasing they are asking about - and asking about
# that phrasing is the template's main use case.
#
# The prompt at config/prompts/compliance_analysis_prompt.md documents the
# analysis contract for an implementation that generates the body with a model
# instead: same inputs, same state keys.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Analysis body used when no KB passage cleared the relevance threshold.
_NO_COVERAGE_ANALYSIS = (
    "The FSA-MRM knowledge base does not contain sufficient coverage to "
    "analyse this question. Rephrase the query with more specific "
    "regulatory terms, or escalate to the compliance team for a manual "
    "review."
)

# Cited excerpt length per passage inside the analysis body.
_POINT_EXCERPT_CHARS = 240

# Human-readable labels for the classification line - deliberately HEDGED
# phrasing only ("appears most related to", never "this is" / "this
# constitutes") so the rule-based generator never emits an unconditional
# verdict by construction (defense-in-depth: OutputFormatNode additionally
# runs a block-definitive-classification scan over the composed text).
_TOPIC_LABELS: Dict[str, str] = {
    "definitive_judgment": ("断定的判断 (definitive/assertive judgment) under 金商法 Article 38-2"),
    "mrm_documentation": "AI Model Risk Management (MRM) documentation requirements",
    "incident_notification": "the 48-hour FSA incident-notification duty",
    "human_oversight": "mandatory human-oversight gates for AI-generated output",
}

# Fixed, topic-specific documentation/remediation checklist. Deterministic -
# never derived from free-form generation, so it cannot itself drift into
# definitive/assertive phrasing.
_TOPIC_CHECKLISTS: Dict[str, List[str]] = {
    "definitive_judgment": [
        "Confirm the AI output uses probabilistic/conditional language, not an absolute promise of outcome.",
        "Verify the Article 38-2 advisory disclosure is attached to any performance-related statement.",
        "Escalate to compliance before the output reaches a customer if it asserts a certain outcome.",
        "Log the review decision and rationale in the MRM audit trail.",
    ],
    "mrm_documentation": [
        "Confirm a current model card exists for the AI system in the model inventory.",
        "Verify validation test results are on file for the deployed model version.",
        "Confirm the model owner and risk tier are documented.",
        "Schedule the next periodic model review per the MRM calendar.",
    ],
    "incident_notification": [
        "Determine whether the event meets the incident materiality threshold.",
        "If material, prepare the FSA notification within the 48-hour window.",
        "Notify the compliance officer and the MRM committee immediately.",
        "Preserve logs and model outputs relevant to the incident for the record.",
    ],
    "human_oversight": [
        "Confirm a qualified reviewer approved the output before any customer-facing use.",
        "Verify the human-oversight gate was not bypassed for this output.",
        "Record the reviewer's decision and rationale.",
        "Escalate any override of an AI recommendation to the designated approver.",
    ],
}

_DEFAULT_CHECKLIST: List[str] = [
    "Route the question to the compliance team for manual topic classification.",
    "Attach the relevant FSA-MRM policy reference once the topic is identified.",
    "Log the query for knowledge-base coverage-gap review.",
]


def _first_sentences(text: str, limit: int) -> str:
    """Trim an excerpt at a sentence boundary where possible, else hard-cap."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    period = cut.rfind(". ")
    if period > limit // 2:
        return cut[: period + 1]
    return cut.rstrip() + "..."


def build_checklist(classification_topic: Optional[str]) -> List[str]:
    """The fixed documentation/remediation checklist for a topic (or the default)."""
    if classification_topic and classification_topic in _TOPIC_CHECKLISTS:
        return list(_TOPIC_CHECKLISTS[classification_topic])
    return list(_DEFAULT_CHECKLIST)


class GenerateAnswerNode(FunctionNode):
    """Rule-based grounded compliance analysis with numbered citations + checklist.

    Input state keys:
        ranked_passages:      JSON list of surviving passages (from RerankFilterNode)
        search_query:          normalised question (for the lead sentence)
        classification_topic:  resolved FSA topic slug, or None

    Output state keys (partial dict):
        compliance_analysis:  analysis body with [n] citation markers (hedged,
                               never an unconditional verdict) - agent-authored
        audited_question:      the caller's question, carried separately so the
                               formatter can render it as quoted material
        citations:             JSON list [{ref, id, title, source}]
        remediation_checklist: JSON list[str] - topic-specific checklist
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        ranked: List[Dict[str, Any]] = from_json(state.get("ranked_passages"), []) or []
        query = state.get("search_query") or ""
        classification_topic = state.get("classification_topic")

        citations: List[Dict[str, Any]] = []
        lines: List[str] = []

        if classification_topic and classification_topic in _TOPIC_LABELS:
            lines.append(
                f"This question appears most related to {_TOPIC_LABELS[classification_topic]} "
                "(indicative classification from keyword matching, not a formal "
                "determination)."
            )
        else:
            lines.append(
                "This question did not match a specific FSA-MRM topic keyword set "
                "(indicative classification only); the analysis below uses the "
                "broader knowledge base."
            )
        lines.append("")

        if not ranked:
            lines.append(_NO_COVERAGE_ANALYSIS)
        else:
            lines.append("Based on the seeded FSA-MRM knowledge base, the following " "passages inform this question:")
            lines.append("")
            for ref, doc in enumerate(ranked, start=1):
                if not isinstance(doc, dict):
                    continue
                title = str(doc.get("title", "")).strip()
                excerpt = _first_sentences(str(doc.get("excerpt", "")), _POINT_EXCERPT_CHARS)
                lines.append(f"[{ref}] {title}: {excerpt}")
                citations.append(
                    {
                        "ref": ref,
                        "id": str(doc.get("id", "")),
                        "title": title,
                        "source": str(doc.get("source", "")),
                    }
                )
        compliance_analysis = "\n".join(lines)
        checklist = build_checklist(classification_topic)

        # Domain audit: grounded compliance analysis assembled.
        emit_trace_event(
            "generate_answer_complete",
            {
                "classification_topic": classification_topic or "unclassified",
                "citation_count": len(citations),
                "analysis_chars": len(compliance_analysis),
                "checklist_items": len(checklist),
                "no_coverage": not ranked,
            },
            state,
        )

        return {
            "compliance_analysis": compliance_analysis,
            "audited_question": query,
            "citations": to_json(citations),
            "remediation_checklist": to_json(checklist),
        }
