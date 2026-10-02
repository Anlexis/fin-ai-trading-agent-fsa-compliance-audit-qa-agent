# FIN-C2-196 — Unit Tests: OutputFormatNode (inner domain node 5, terminal)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# formatted_answer is a domain field; the legal disclaimer and the
# definitive-verdict control are both part of THIS node's output contract.
#
# Mirrors docs/03_test_spec.md §2.6 (FMT-01..FMT-05, plus the tests for the
# template's own output invariant).
# Deterministic — no model call, no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.output_format_node import OutputFormatNode
from src.schemas.state import to_json

_DISCLAIMER_FRAGMENT = "It is not legal advice, not a formal regulatory determination, and not " "investment advice"
_BLOCK_FRAGMENT = "withheld by the block-definitive-classification control"
_WITHHELD_FRAGMENT = "No analysis was released for this request"


def _make_state(compliance_analysis, citations, **extra) -> dict:
    state = {
        "compliance_analysis": compliance_analysis,
        "citations": citations,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestFormattedAnswer:
    def test_fmt_01_composes_header_body_sources_checklist_disclaimer(self):
        citations = to_json(
            [
                {
                    "ref": 1,
                    "id": "fsa-mrm-001",
                    "title": "Article 38-2 prohibition",
                    "source": "金融商品取引法 Article 38-2, overview note",
                }
            ]
        )
        checklist = to_json(["Confirm the AI output uses probabilistic/conditional language."])
        result = OutputFormatNode()(
            _make_state("[1] the grounded compliance analysis body.", citations, remediation_checklist=checklist)
        )
        answer = result["formatted_answer"]
        assert answer.startswith("# FSA Trading Compliance Audit Result")
        assert "[1] the grounded compliance analysis body." in answer
        assert "## Sources" in answer
        assert "- [1] Article 38-2 prohibition (金融商品取引法 Article 38-2, overview note)" in answer
        assert "## Documentation / Remediation Checklist" in answer
        assert "- [ ] Confirm the AI output uses probabilistic/conditional language." in answer
        assert _DISCLAIMER_FRAGMENT in answer
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain string, never the enum.
        assert isinstance(result["status"], str)

    def test_fmt_02_source_suffix_omitted_when_blank(self):
        citations = to_json([{"ref": 1, "id": "fsa-mrm-001", "title": "Article 38-2 prohibition", "source": ""}])
        answer = OutputFormatNode()(_make_state("body.", citations))["formatted_answer"]
        assert "- [1] Article 38-2 prohibition\n" in answer + "\n"
        assert "()" not in answer

    def test_fmt_03_disclaimer_present_on_every_answer(self):
        # The disclaimer must ride WITH the substance, never separately.
        for analysis in ("a body.", ""):
            answer = OutputFormatNode()(_make_state(analysis, to_json([])))["formatted_answer"]
            assert _DISCLAIMER_FRAGMENT in answer


class TestDegradedInputs:
    def test_fmt_04_no_citations_renders_explicit_none_line(self):
        answer = OutputFormatNode()(_make_state("no coverage body.", to_json([])))["formatted_answer"]
        assert "- none (no knowledge-base passage cleared the relevance threshold)" in answer

    def test_no_checklist_renders_explicit_none_line(self):
        answer = OutputFormatNode()(_make_state("body.", to_json([])))["formatted_answer"]
        assert "## Documentation / Remediation Checklist" in answer
        assert "- none" in answer

    def test_fmt_05_missing_analysis_uses_fallback_text(self):
        state = _make_state("", to_json([]))
        del state["compliance_analysis"]
        result = OutputFormatNode()(state)
        assert "No analysis is available for this request." in result["formatted_answer"]
        assert result["status"] == AgentStatus.SUCCESS.value


class TestBlockDefinitiveClassification:
    """DOMAIN_BRIEF security control: an unconditional, non-suppressible scan
    for absolute/assertive verdict phrasing. Verified against the real regex
    set (never `assert True` / hand-waved)."""

    def test_guarantee_phrasing_is_blocked_and_replaced(self):
        analysis = "[1] we guarantee this fund will outperform the benchmark."
        result = OutputFormatNode()(_make_state(analysis, to_json([])))
        answer = result["formatted_answer"]
        assert "we guarantee this fund will outperform" not in answer
        assert _BLOCK_FRAGMENT in answer
        # The control substitutes text; it does NOT flip the node to ERROR —
        # that is PostProcessNode's job, for a different violation class.
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_without_a_doubt_phrasing_is_blocked(self):
        analysis = "this is without a doubt a breach of Article 38-2."
        answer = OutputFormatNode()(_make_state(analysis, to_json([])))["formatted_answer"]
        assert _BLOCK_FRAGMENT in answer

    def test_japanese_teidan_phrasing_is_blocked(self):
        analysis = "この行為はと断定される。"
        answer = OutputFormatNode()(_make_state(analysis, to_json([])))["formatted_answer"]
        assert _BLOCK_FRAGMENT in answer

    def test_hedged_analysis_passes_through_unblocked(self):
        analysis = "[1] this appears related to definitive-judgment concerns, pending manual review."
        answer = OutputFormatNode()(_make_state(analysis, to_json([])))["formatted_answer"]
        assert _BLOCK_FRAGMENT not in answer
        assert "this appears related to definitive-judgment concerns" in answer

    def test_block_still_carries_the_disclaimer(self):
        # Even a withheld analysis must still ship the standing disclaimer.
        analysis = "we guarantee this outcome"
        answer = OutputFormatNode()(_make_state(analysis, to_json([])))["formatted_answer"]
        assert _BLOCK_FRAGMENT in answer
        assert _DISCLAIMER_FRAGMENT in answer


class TestAudit:
    def test_output_format_complete_records_the_block_flag(self, monkeypatch):
        from unittest.mock import MagicMock

        import src.nodes.output_format_node as ofn

        spy = MagicMock()
        monkeypatch.setattr(ofn, "emit_trace_event", spy)
        OutputFormatNode()(_make_state("we guarantee this outcome", to_json([])))
        events = [call.args[0] for call in spy.call_args_list]
        assert "output_format_complete" in events
        payload = spy.call_args_list[events.index("output_format_complete")].args[1]
        assert payload["definitive_verdict_blocked_in_analysis"] is True
        assert payload["disclaimer_present"] is True


class TestOutputInvariantHoldsForEveryRepresentation:
    """The template states two output rules; both are enforced on the COMPOSED
    answer, not only on the fragment that produced it.

    Checking the analysis body alone would leave the classification line, the
    checklist and the rendered source titles unchecked — every other
    representation of the same output. An invariant that holds only for the
    convenient representation is not an invariant.
    """

    def test_disclaimer_is_present_on_every_answer(self):
        for analysis in ("a grounded analysis", "", "no coverage available"):
            result = OutputFormatNode()(_make_state(analysis, to_json([])))
            assert _DISCLAIMER_FRAGMENT in result["formatted_answer"]

    def test_a_verdict_reaching_the_answer_through_a_source_title_is_caught(self):
        # A citation title is composed into the answer AFTER the body scan, so
        # it is a second route to the same output. It is agent-authored text.
        citations = to_json(
            [{"ref": 1, "id": "x", "title": "This wording constitutes definitively a breach", "source": "s"}]
        )
        result = OutputFormatNode()(_make_state("a hedged analysis", citations))
        assert result["status"] == AgentStatus.ERROR.value
        assert _WITHHELD_FRAGMENT in result["formatted_answer"]

    def test_a_verdict_reaching_the_answer_through_a_checklist_item_is_caught(self):
        checklist = to_json(["We guarantee the outcome of this review."])
        result = OutputFormatNode()(_make_state("a hedged analysis", to_json([]), remediation_checklist=checklist))
        assert result["status"] == AgentStatus.ERROR.value

    def test_a_withheld_answer_releases_no_fragment_of_the_original(self):
        marker = "PROPRIETARY ANALYSIS BODY"
        checklist = to_json([f"{marker} — we guarantee the outcome."])
        result = OutputFormatNode()(_make_state("a hedged analysis", to_json([]), remediation_checklist=checklist))
        assert marker not in result["formatted_answer"]

    def test_a_composition_that_drops_the_disclaimer_withholds_the_answer(self, monkeypatch):
        # Verify-the-verifier: the disclaimer check must be able to FAIL.
        # Only the RENDERER is replaced here, so the check still knows what it
        # is looking for and can report that it did not arrive.
        import src.nodes.output_format_node as ofn

        monkeypatch.setattr(ofn, "_render_disclaimer", lambda: "*(disclaimer omitted)*")
        result = OutputFormatNode()(_make_state("a hedged analysis", to_json([])))
        assert result["status"] == AgentStatus.ERROR.value
        assert _WITHHELD_FRAGMENT in result["formatted_answer"]


class TestTheControlDoesNotFireOnTheCallersOwnWords:
    """The fail-CLOSED direction, and the one that blocks real work.

    The control polices what this agent ASSERTS. The caller's question is
    rendered as quoted material and is excluded — a compliance officer quoting
    the very phrasing they are asking about is the template's main use case,
    and scanning their words would suppress the answer to exactly the question
    the template exists to answer.
    """

    def test_a_question_quoting_prohibited_phrasing_is_still_answered(self):
        question = "Does wording that guarantees a certain profit breach Article 38-2?"
        result = OutputFormatNode()(_make_state("a hedged, grounded analysis", to_json([]), audited_question=question))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _BLOCK_FRAGMENT not in result["formatted_answer"]
        assert _WITHHELD_FRAGMENT not in result["formatted_answer"]

    def test_the_question_is_rendered_as_attributed_quoted_material(self):
        question = "does this wording risk a definitive judgment"
        answer = OutputFormatNode()(_make_state("a hedged analysis", to_json([]), audited_question=question))[
            "formatted_answer"
        ]
        assert "## Question under audit" in answer
        assert f"> {question}" in answer

    def test_an_agent_authored_verdict_is_still_blocked_alongside_it(self):
        # Both directions in one state: the exclusion must not become a hole.
        question = "Does wording that guarantees a certain profit breach Article 38-2?"
        result = OutputFormatNode()(
            _make_state("This wording constitutes definitively a breach.", to_json([]), audited_question=question)
        )
        assert _BLOCK_FRAGMENT in result["formatted_answer"]


class TestCallerReferenceRendersInert:
    def test_a_validated_case_ref_is_rendered(self):
        answer = OutputFormatNode()(_make_state("a hedged analysis", to_json([]), case_ref="case_2026_0831"))[
            "formatted_answer"
        ]
        assert "case_2026_0831" in answer

    def test_no_reference_line_when_none_was_supplied(self):
        answer = OutputFormatNode()(_make_state("a hedged analysis", to_json([])))["formatted_answer"]
        assert "Audit reference" not in answer
