# FIN-C2-196 — Unit Tests: retrieval quality over the seeded FSA-MRM KB
#
# Golden-query suite: drives the REAL inner retrieval chain
# (InputValidateNode → RetrieveNode → RerankFilterNode) via node(state) /
# __call__ (ANONYMOUS inner nodes) against config/kb/fsa_mrm_kb.json and pins
# the expected top hit per domain query. InputValidateNode auto-classifies
# the topic from keywords, and that classification FEEDS FORWARD as a
# retrieval-pool filter (RetrieveNode) + rerank boost (RerankFilterNode) — so
# expected ids/scores below were captured by RUNNING the actual three-node
# chain (not a standalone reimplementation of the scorer), to avoid a
# calibration/reality mismatch.
#
# Mirrors docs/03_test_spec.md §2.9 (QUAL-01..QUAL-07).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

import json
import pathlib

import pytest

from framework.schemas.trust_level import TrustLevel

from src.nodes.generate_answer_node import GenerateAnswerNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.rerank_filter_node import RerankFilterNode
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import from_json

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_KB_IDS = {
    entry["id"] for entry in json.loads((_ROOT / "config" / "kb" / "fsa_mrm_kb.json").read_text(encoding="utf-8"))
}

_DEFAULT_SCORE_THRESHOLD = 0.25  # mirrors config/agent.yaml retrieval block


def _search(payload: str) -> list[dict]:
    """Run the real inner retrieval chain and return the surviving passages."""
    state = {
        "validated_input": payload,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "quality-session",
        "execution_time": {},
    }
    state.update(InputValidateNode()(state))
    state.update(RetrieveNode()(state))
    state.update(RerankFilterNode()(state))
    return from_json(state["ranked_passages"], [])


# (query, expected top-1 KB entry id) — captured from a real run of the three-node chain.
_GOLDEN_QUERIES = [
    (
        "does an ai trading recommendation risk a definitive judgment under " "article 38-2",
        "fsa-mrm-002",
    ),
    ("what documentation should exist for an ai trading model", "fsa-mrm-004"),
    ("when must an ai trading incident be reported to the regulator", "fsa-mrm-007"),
    ("what human oversight is required before a customer sees ai output", "fsa-mrm-010"),
    ("model inventory validation cadence for production models", "fsa-mrm-005"),
    (
        "escalation and override procedure when a reviewer rejects an ai " "recommendation",
        "fsa-mrm-011",
    ),
    ("evidence preservation duties during an incident review", "fsa-mrm-009"),
    ("materiality threshold for reporting an ai incident", "fsa-mrm-008"),
]


class TestGoldenQueries:
    @pytest.mark.parametrize(("query", "expected_id"), _GOLDEN_QUERIES)
    def test_qual_01_top_hit_per_golden_query(self, query, expected_id):
        kept = _search(query)
        assert kept, f"no passage cleared the relevance floor for: {query!r}"
        assert kept[0]["id"] == expected_id

    def test_qual_02_all_survivors_clear_the_relevance_floor(self):
        for query, _expected in _GOLDEN_QUERIES:
            for doc in _search(query):
                assert doc["score"] >= _DEFAULT_SCORE_THRESHOLD

    def test_qual_03_survivor_ids_exist_in_the_seeded_kb(self):
        for query, _expected in _GOLDEN_QUERIES:
            for doc in _search(query):
                assert doc["id"] in _KB_IDS


class TestPrecision:
    def test_qual_04_definitive_judgment_query_excludes_other_topics(self):
        # The auto-classified topic filter must keep OTHER-category entries
        # out even though they share generic AI/trading vocabulary.
        kept = _search(_GOLDEN_QUERIES[0][0])
        assert {d["id"] for d in kept} == {"fsa-mrm-001", "fsa-mrm-002", "fsa-mrm-003"}
        assert {d["category"] for d in kept} == {"definitive_judgment"}

    def test_qual_05_topic_override_restricts_to_that_topic(self):
        payload = json.dumps({"query": "model documentation and validation requirements", "topic": "mrm_documentation"})
        kept = _search(payload)
        assert kept, "mrm_documentation topic carries seeded entries"
        assert {d["category"] for d in kept} == {"mrm_documentation"}
        assert kept[0]["id"] == "fsa-mrm-005"


class TestNoCoverage:
    def test_qual_06_out_of_domain_query_yields_no_survivors(self):
        assert _search("quantum telepathy sandwich recipes") == []

    def test_qual_07_no_coverage_produces_the_escalation_analysis(self):
        state = {
            "ranked_passages": "[]",
            "search_query": "quantum telepathy sandwich recipes",
            "classification_topic": None,
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "node_history": [],
            "error_log": [],
            "session_id": "quality-session",
            "execution_time": {},
        }
        result = GenerateAnswerNode()(state)
        assert "does not contain sufficient coverage" in result["compliance_analysis"]
        assert from_json(result["citations"]) == []
