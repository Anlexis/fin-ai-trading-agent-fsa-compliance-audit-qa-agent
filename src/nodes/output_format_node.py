"""AgentCore Platform v1.0"""

# FIN-C2-196 - OutputFormatNode
# Domain node 5 (terminal): compose the final formatted answer - the
# classification line + grounded compliance analysis, the audited question,
# the Sources list, the Documentation / Remediation Checklist, and the
# standing non-suppressible legal disclaimer.
#
# THIS NODE OWNS THE TEMPLATE'S OUTPUT INVARIANT, in two parts:
#
#   (1) every answer carries the legal disclaimer, and
#   (2) this agent never itself states an unconditional verdict.
#
# Neither is suppressible: no flag, argument or state field reaches either
# control, and both are re-verified against the COMPOSED answer rather than
# against the fragment that produced it. Checking only the analysis body would
# leave the classification line, the checklist and the source titles - every
# other representation of the same output - unchecked, and an invariant that
# holds for the convenient representation is not an invariant.
#
# WHAT THE VERDICT CONTROL POLICES, AND WHAT IT DOES NOT. It polices text this
# agent AUTHORED: the analysis body, the classification line, the checklist and
# the rendered source titles. It does NOT police the caller's own question,
# which is rendered as quoted material. The distinction is load-bearing rather
# than cosmetic: a compliance officer asking whether wording that "guarantees"
# a return is a prohibited definitive judgment is the template's central use
# case, and scanning their question would suppress the answer to exactly the
# question the template exists to answer.
#
# Wired by the inner graph (DomainWorkflowGraph). get_output() of the inner
# graph surfaces formatted_answer + status + the structured fields to the
# outer merge_output().
# Returns only changed state keys (partial dict).

import re
from typing import Any, ClassVar, Dict, List, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# Standing, non-suppressible legal disclaimer - appended to EVERY answer this
# template emits. No flag, argument, or state field can suppress it.
_LEGAL_DISCLAIMER = (
    "This analysis is generated from the seeded FSA-MRM knowledge base for "
    "informational purposes only. It is not legal advice, not a formal "
    "regulatory determination, and not investment advice — consult "
    "qualified counsel and verify against the primary regulatory text "
    "before acting on it."
)

# Absolute/assertive verdict phrasing the agent must never itself emit.
#
# Every pattern requires a VERDICT CONSTRUCTION - an assertion verb bound to
# its object - not the mere mention of a regulatory term. That is a deliberate
# constraint rather than a stylistic one. An unanchored keyword list fires on
# ordinary domain vocabulary: "guarantee" is simultaneously the word this
# template classifies questions BY (it is a keyword for the definitive-judgment
# topic) and, in a bare form, the word that would block the answer - so a bare
# `\bguarantee[sd]?\b` withheld the analysis for the single most common
# question the template receives. Each pattern below therefore binds the
# assertive adverb or verb to what is being asserted.
_DEFINITIVE_VERDICT_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\bdefinitely (is|constitutes|violates|breaches)\b", re.IGNORECASE),
    # "we guarantee ...", "this guarantees a return" - an assertion by the
    # agent. A question ABOUT guarantees does not match.
    re.compile(
        r"\b(?:we|this|it|the (?:output|statement|wording|fund|product))\s+" r"guarantee[sd]?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bis\s+guaranteed\s+to\b", re.IGNORECASE),
    re.compile(r"\bwithout (a )?doubt\b", re.IGNORECASE),
    re.compile(r"\bconclusively (is|constitutes|proves|violates)\b", re.IGNORECASE),
    re.compile(r"\bcertainly (is|constitutes|violates|breaches)\b", re.IGNORECASE),
    re.compile(r"\b(is|constitutes) definitively\b", re.IGNORECASE),
    re.compile(r"\bunconditionally (is|constitutes|guarantees?)\b", re.IGNORECASE),
    re.compile(r"と断定"),
    re.compile(r"であると断定"),
]

# Emitted in place of the answer when the composed output breaches the
# template's own invariant. It carries no fragment of the withheld text.
_INVARIANT_WITHHELD_NOTICE = (
    "[No analysis was released for this request: the composed answer did not "
    "satisfy this agent's output rules. Escalate the question to a human "
    "compliance reviewer for a manual determination.]"
)


def _render_disclaimer() -> str:
    """Render the disclaimer line appended to every answer.

    Deliberately a separate function from the check that verifies the
    disclaimer arrived: a renderer that also defines what "correct" means
    cannot fail its own test. Keeping them apart is what lets the verification
    below detect a composition that dropped the line.
    """
    return f"*{_LEGAL_DISCLAIMER}*"


def _authored_text(formatted_answer: str, audited_question: str) -> str:
    """Return the composed answer with the quoted caller question removed.

    The verdict control applies to what the AGENT states. The caller's own
    words are rendered in the report as quoted material and are removed here
    before the check, so a question that quotes prohibited phrasing - the
    template's most common request - is answered rather than suppressed.
    """
    question = (audited_question or "").strip()
    if not question:
        return formatted_answer
    return formatted_answer.replace(_quote_block(question), "")


_DEFINITIVE_BLOCK_FALLBACK = (
    "[Analysis withheld by the block-definitive-classification control - the "
    "generated text used unconditional verdict phrasing, which this agent "
    "must never emit. Escalate this question to a human compliance "
    "reviewer for a manual determination.]"
)


def _find_definitive_verdict(text: str) -> bool:
    """True when the text states an unconditional verdict."""
    return any(pattern.search(text) for pattern in _DEFINITIVE_VERDICT_PATTERNS)


def _block_definitive_classification(text: str) -> Tuple[bool, str]:
    """Scan agent-authored text for assertive/unconditional verdict phrasing.

    Returns (blocked, safe_text). Unconditional - always runs, no suppression
    path. On a match the ENTIRE body is replaced with a safe
    hedge-and-escalate fallback, never a partial edit, so a downstream reader
    can never be shown a definitive fragment with the rest removed around it.
    """
    if _find_definitive_verdict(text):
        return True, _DEFINITIVE_BLOCK_FALLBACK
    return False, text


def _quote_block(text: str) -> str:
    """Render caller-supplied text as an inert Markdown quote.

    The question has already been whitespace-collapsed at intake, so it cannot
    contain a line break and cannot open a heading or a list item. Prefixing it
    with a quote marker states its provenance for a reader: this is what was
    asked, not what the agent concluded.
    """
    return f"> {text}"


class OutputFormatNode(FunctionNode):
    """Compose the final answer and enforce the template's output invariant.

    Input state keys:
        compliance_analysis:   analysis body with [n] citation markers
        audited_question:       the caller's question (rendered as quoted text)
        citations:              JSON list [{ref, id, title, source}]
        remediation_checklist:  JSON list[str]
        classification_topic:   resolved topic slug, or None
        case_ref:               validated inert audit reference, or None

    Output state keys (partial dict):
        formatted_answer: final rendered answer string
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
                          (plain strings - never the bare enum in State)
        error_log:        (on error) list of error messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        analysis = state.get("compliance_analysis") or ("No analysis is available for this request.")
        citations: List[Dict[str, Any]] = from_json(state.get("citations"), []) or []
        checklist: List[str] = from_json(state.get("remediation_checklist"), []) or []
        audited_question = state.get("audited_question") or ""
        case_ref = state.get("case_ref")

        # First pass, on the agent-authored analysis body.
        blocked, analysis = _block_definitive_classification(analysis)

        lines: List[str] = []
        lines.append("# FSA Trading Compliance Audit Result")
        lines.append("")
        if isinstance(case_ref, str) and case_ref:
            # Already validated to the inert identifier alphabet at intake.
            lines.append(f"Audit reference: `{case_ref}`")
            lines.append("")
        if isinstance(audited_question, str) and audited_question.strip():
            lines.append("## Question under audit")
            lines.append("")
            lines.append(_quote_block(audited_question.strip()))
            lines.append("")
        lines.append(analysis)
        lines.append("")
        lines.append("## Sources")
        if citations:
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                ref = citation.get("ref", "?")
                title = str(citation.get("title", "")).strip()
                source = str(citation.get("source", "")).strip()
                suffix = f" ({source})" if source else ""
                lines.append(f"- [{ref}] {title}{suffix}")
        else:
            lines.append("- none (no knowledge-base passage cleared the relevance threshold)")
        lines.append("")
        lines.append("## Documentation / Remediation Checklist")
        if checklist:
            for item in checklist:
                lines.append(f"- [ ] {item}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(_render_disclaimer())

        formatted_answer = "\n".join(lines)

        # Second pass, over the COMPOSED answer with the quoted caller question
        # excluded. Everything the agent authored - the classification line, the
        # analysis, the checklist, the rendered source titles - is checked in the
        # form the caller will actually receive, not only in the fragment that
        # produced it. Composition is where a verdict could reappear: a source
        # title or a checklist item is as much a statement by this agent as the
        # analysis body is.
        authored = _authored_text(formatted_answer, audited_question)
        if _find_definitive_verdict(authored):
            return self._withhold(
                state,
                "definitive_verdict_in_composed_answer",
                "the composed answer stated an unconditional verdict",
            )

        # The disclaimer is part of the invariant, so its presence is VERIFIED
        # rather than assumed from the fact that it was appended above. An
        # answer that reached a caller without it would breach the same
        # contract as a definitive verdict.
        if _LEGAL_DISCLAIMER not in formatted_answer:
            return self._withhold(
                state,
                "disclaimer_missing",
                "the composed answer did not carry the legal disclaimer",
            )

        # Final answer composed: disclaimer verified present, no unconditional
        # verdict in any agent-authored part. The payload records the verdict of
        # each control, never the text they inspected.
        emit_trace_event(
            "output_format_complete",
            {
                "answer_chars": len(formatted_answer),
                "citation_count": len(citations),
                "checklist_items": len(checklist),
                "definitive_verdict_blocked_in_analysis": blocked,
                "disclaimer_present": True,
                "has_case_ref": bool(case_ref),
            },
            state,
        )

        return {
            "formatted_answer": formatted_answer,
            "status": AgentStatus.SUCCESS.value,
        }

    def _withhold(self, state: AgentState, reason: str, message: str) -> Dict[str, Any]:
        """Fail closed: publish no answer at all when the invariant is breached.

        The answer text is not returned in any form. Emitting it alongside an
        error status would ship exactly the content the control exists to
        withhold, with a label attached.
        """
        emit_trace_event("output_format_withheld", {"reason": reason}, state)
        return {
            "formatted_answer": _INVARIANT_WITHHELD_NOTICE,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"OutputFormatNode: answer withheld - {message}."],
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            "formatted_output": "Request could not be completed. "
            + (f"OutputFormatNode: answer withheld - {message}."),
        }
