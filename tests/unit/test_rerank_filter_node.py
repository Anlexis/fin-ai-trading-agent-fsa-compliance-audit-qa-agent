# FIN-C2-196 — Unit Tests: RerankFilterNode (inner domain node 3)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller
# — no carve-out: execute(self, state) takes
# no `config` parameter. Threshold / top_k overrides are exercised by seeding
# the state-level `retrieval_config` field, still invoked through node(state).
#
# Mirrors docs/03_test_spec.md §2.4 (RRF-01..RRF-08).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.rerank_filter_node import RerankFilterNode
from src.schemas.state import from_json, to_json


def _doc(doc_id, score, category="definitive_judgment"):
    return {
        "id": doc_id,
        "title": f"entry {doc_id}",
        "category": category,
        "source": "seeded kb",
        "score": score,
        "excerpt": "excerpt text",
    }


def _make_state(candidates, **extra) -> dict:
    state = {
        "retrieved_passages": to_json(candidates),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestThresholdAndCap:
    def test_rrf_01_default_threshold_drops_weak_candidates(self):
        result = RerankFilterNode()(_make_state([_doc("fsa-a", 0.9), _doc("fsa-b", 0.1)]))
        kept = from_json(result["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a"]  # 0.1 < default 0.25 floor

    def test_rrf_02_state_score_threshold_override(self):
        state = _make_state(
            [_doc("fsa-a", 0.9), _doc("fsa-b", 0.3)],
            retrieval_config=to_json({"score_threshold": 0.5}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a"]

    def test_rrf_03_state_top_k_override(self):
        state = _make_state(
            [_doc("fsa-a", 0.9), _doc("fsa-b", 0.8), _doc("fsa-c", 0.7)],
            retrieval_config=to_json({"top_k": 1}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a"]

    def test_ranked_passages_is_json_string(self):
        # List-shaped State fields travel as JSON strings.
        result = RerankFilterNode()(_make_state([_doc("fsa-a", 0.9)]))
        assert isinstance(result["ranked_passages"], str)


class TestTopicBoost:
    def test_rrf_04_matching_topic_is_boosted_and_reranked(self):
        state = _make_state(
            [_doc("fsa-a", 0.30, category="human_oversight"), _doc("fsa-b", 0.25, category="mrm_documentation")],
            query_filters=to_json({"topic": "mrm_documentation", "top_k": None}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-b", "fsa-a"]
        assert kept[0]["score"] == 0.35  # 0.25 + 0.1 topic boost

    def test_rrf_05_boost_is_capped_at_one(self):
        state = _make_state(
            [_doc("fsa-a", 0.95, category="mrm_documentation")],
            query_filters=to_json({"topic": "mrm_documentation", "top_k": None}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert kept[0]["score"] == 1.0


class TestCallerTopK:
    def test_rrf_06_stricter_caller_top_k_wins(self):
        state = _make_state(
            [_doc("fsa-a", 0.9), _doc("fsa-b", 0.8), _doc("fsa-c", 0.7)],
            query_filters=to_json({"topic": None, "top_k": 1}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a"]

    def test_rrf_06_looser_caller_top_k_does_not_widen(self):
        state = _make_state(
            [_doc("fsa-a", 0.9), _doc("fsa-b", 0.8), _doc("fsa-c", 0.7)],
            query_filters=to_json({"topic": None, "top_k": 10}),
            retrieval_config=to_json({"top_k": 2, "score_threshold": 0.25}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a", "fsa-b"]


class TestRobustness:
    def test_rrf_07_garbage_candidates_are_skipped_or_dropped(self):
        candidates = [
            "not-a-dict",
            {"id": "fsa-bad", "title": "b", "category": "x", "source": "s", "score": "NaN?", "excerpt": "e"},
            _doc("fsa-a", 0.9),
        ]
        kept = from_json(RerankFilterNode()(_make_state(candidates))["ranked_passages"])
        # The string entry is skipped; the uncoercible score becomes 0.0 and
        # falls below the relevance floor.
        assert [d["id"] for d in kept] == ["fsa-a"]

    def test_rrf_08_deterministic_tie_break_by_id(self):
        kept = from_json(RerankFilterNode()(_make_state([_doc("fsa-b", 0.5), _doc("fsa-a", 0.5)]))["ranked_passages"])
        assert [d["id"] for d in kept] == ["fsa-a", "fsa-b"]
