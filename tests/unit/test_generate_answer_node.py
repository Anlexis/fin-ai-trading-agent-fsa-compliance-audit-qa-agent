# FIN-C2-196 — Unit Tests: GenerateAnswerNode (inner domain node 4)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# compliance_analysis / citations / remediation_checklist are DOMAIN fields
# (not input-scan targets — only user_input/validated_input/llm_response are
# scanned), so Title-Case KB titles inside them are safe to assert on.
#
# Mirrors docs/03_test_spec.md §2.5 (GEN-01..GEN-05).
# Deterministic — rule-assembled from ranked_passages + classification_topic
# only (grounded by construction; no LLM, no network). framework.* / src.*
# imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.generate_answer_node import GenerateAnswerNode, build_checklist
from src.schemas.state import from_json, to_json


def _ranked(*entries):
    return to_json(list(entries))


def _doc(doc_id, title, excerpt, source="seeded kb"):
    return {
        "id": doc_id,
        "title": title,
        "category": "definitive_judgment",
        "source": source,
        "score": 0.9,
        "excerpt": excerpt,
    }


def _make_state(ranked_passages, query="does this recommendation risk a definitive judgment", **extra) -> dict:
    state = {
        "ranked_passages": ranked_passages,
        "search_query": query,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestGroundedAnswer:
    def test_gen_01_answer_carries_numbered_citation_markers(self):
        ranked = _ranked(
            _doc(
                "fsa-mrm-001",
                "Article 38-2 prohibition on definitive judgments in solicitation",
                "a firm must not provide a definitive judgment.",
            ),
            _doc(
                "fsa-mrm-002",
                "AI-generated output and the definitive-judgment prohibition",
                "review for absolute or promissory phrasing.",
            ),
        )
        result = GenerateAnswerNode()(_make_state(ranked))
        answer = result["compliance_analysis"]
        assert "[1] Article 38-2 prohibition on definitive judgments in solicitation:" in answer
        assert "[2] AI-generated output and the definitive-judgment prohibition:" in answer

    def test_gen_02_query_is_carried_separately_not_inlined(self):
        # The caller's question is emitted as its own field so the formatter can
        # render it as quoted material. It must NOT appear inside the analysis
        # body: the definitive-verdict control scans that body, and text the
        # caller wrote is not something this agent asserted.
        ranked = _ranked(_doc("fsa-mrm-001", "Article 38-2 prohibition", "excerpt."))
        query = "does this wording risk a definitive judgment"
        result = GenerateAnswerNode()(_make_state(ranked, query=query))
        assert result["audited_question"] == query
        assert query not in result["compliance_analysis"]
        assert "passages inform this question" in result["compliance_analysis"]

    def test_gen_03_citations_mirror_ranked_order(self):
        ranked = _ranked(
            _doc("fsa-mrm-001", "Article 38-2 prohibition", "a.", source="金融商品取引法 Article 38-2, overview note"),
            _doc("fsa-mrm-002", "AI-generated output prohibition", "b."),
        )
        citations = from_json(GenerateAnswerNode()(_make_state(ranked))["citations"])
        assert [c["ref"] for c in citations] == [1, 2]
        assert [c["id"] for c in citations] == ["fsa-mrm-001", "fsa-mrm-002"]
        assert citations[0]["source"] == "金融商品取引法 Article 38-2, overview note"

    def test_citations_is_json_string(self):
        # List-shaped State fields travel as JSON strings.
        ranked = _ranked(_doc("fsa-mrm-001", "Article 38-2 prohibition", "a."))
        result = GenerateAnswerNode()(_make_state(ranked))
        assert isinstance(result["citations"], str)

    def test_gen_04_answer_is_grounded_in_ranked_passages_only(self):
        ranked = _ranked(_doc("fsa-mrm-001", "Article 38-2 prohibition", "settled fact is the core violation pattern."))
        answer = GenerateAnswerNode()(_make_state(ranked))["compliance_analysis"]
        # Every content line traces to the single ranked passage.
        assert "settled fact is the core violation pattern." in answer
        assert "[2]" not in answer


class TestTopicClassificationLine:
    def test_gen_classified_topic_renders_hedged_label(self):
        result = GenerateAnswerNode()(_make_state(_ranked(), classification_topic="definitive_judgment"))
        answer = result["compliance_analysis"]
        assert answer.startswith("This question appears most related to")
        assert "断定的判断" in answer
        assert "indicative classification from keyword matching, not a formal determination" in answer

    def test_gen_unclassified_topic_renders_fallback_line(self):
        result = GenerateAnswerNode()(_make_state(_ranked(), classification_topic=None))
        answer = result["compliance_analysis"]
        assert "did not match a specific FSA-MRM topic keyword set" in answer


class TestNoCoverage:
    def test_gen_05_empty_ranked_set_yields_no_coverage_analysis(self):
        result = GenerateAnswerNode()(_make_state(_ranked()))
        assert "does not contain sufficient coverage" in result["compliance_analysis"]
        assert from_json(result["citations"]) == []

    def test_missing_ranked_field_is_treated_as_no_coverage(self):
        state = _make_state(None)
        del state["ranked_passages"]
        result = GenerateAnswerNode()(state)
        assert "does not contain sufficient coverage" in result["compliance_analysis"]


class TestRemediationChecklist:
    """The checklist is FIXED per-topic (never derived from free-form
    generation), independent of the ranked-passage set."""

    def test_topic_specific_checklist_is_selected(self):
        result = GenerateAnswerNode()(_make_state(_ranked(), classification_topic="incident_notification"))
        checklist = from_json(result["remediation_checklist"])
        assert checklist == build_checklist("incident_notification")
        assert any("48-hour" in item for item in checklist)

    def test_unclassified_topic_falls_back_to_default_checklist(self):
        result = GenerateAnswerNode()(_make_state(_ranked(), classification_topic=None))
        checklist = from_json(result["remediation_checklist"])
        assert checklist == build_checklist(None)
        assert any("manual topic classification" in item for item in checklist)

    def test_build_checklist_has_all_four_fixed_topics(self):
        for topic in (
            "definitive_judgment",
            "mrm_documentation",
            "incident_notification",
            "human_oversight",
        ):
            checklist = build_checklist(topic)
            assert len(checklist) == 4
