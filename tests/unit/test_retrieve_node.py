# FIN-C2-196 — Unit Tests: RetrieveNode (inner domain node 2)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller
# — no carve-out: execute(self, state) takes
# no `config` parameter, so the ONLY way to exercise a config knob is to seed
# the state-level `retrieval_config` field and invoke via node(state).
#
# Expected scores below are read off the REAL seeded KB (config/kb/
# fsa_mrm_kb.json, 12 entries, 4 categories) via the actual scoring function —
# not hand-computed — so a KB or scorer regression fails this file, not a
# stale hand-written number.
#
# Mirrors docs/03_test_spec.md §2.3 (RET-01..RET-08).
# Deterministic — keyword scoring over the seeded KB; no LLM, no network.
# framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import from_json, to_json

_DEFINITIVE_QUERY = "does an ai trading recommendation risk a definitive judgment under article 38-2"
_MRM_DOC_QUERY = "model documentation and validation requirements"


def _make_state(query=_DEFINITIVE_QUERY, **extra) -> dict:
    state = {
        "search_query": query,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestRetrieveHappyPath:
    def test_ret_01_top_hit_is_the_definitive_judgment_entry(self):
        result = RetrieveNode()(_make_state())
        docs = from_json(result["retrieved_passages"])
        assert docs, "expected candidates for the definitive-judgment query"
        assert docs[0]["id"] == "fsa-mrm-002"

    def test_ret_02_scores_sorted_descending(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_passages"])
        scores = [d["score"] for d in docs]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0.0 for s in scores)

    def test_ret_03_entry_shape_and_excerpt_cap(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_passages"])
        for doc in docs:
            assert set(doc.keys()) == {"id", "title", "category", "source", "score", "excerpt"}
            assert len(doc["excerpt"]) <= 400

    def test_retrieved_passages_is_json_string(self):
        # List-shaped State fields travel as JSON strings.
        result = RetrieveNode()(_make_state())
        assert isinstance(result["retrieved_passages"], str)


class TestRetrieveFilters:
    def test_ret_04_topic_filter_restricts_pool(self):
        state = _make_state(
            query=_MRM_DOC_QUERY,
            query_filters=to_json({"topic": "mrm_documentation", "top_k": None}),
        )
        docs = from_json(RetrieveNode()(state)["retrieved_passages"])
        assert docs, "mrm_documentation topic has seeded entries"
        assert {d["category"] for d in docs} == {"mrm_documentation"}
        assert docs[0]["id"] == "fsa-mrm-005"

    def test_ret_05_empty_query_yields_no_candidates(self):
        docs = from_json(RetrieveNode()(_make_state(query=""))["retrieved_passages"])
        assert docs == []

    def test_out_of_domain_query_yields_no_candidates(self):
        docs = from_json(RetrieveNode()(_make_state(query="quantum telepathy sandwich recipes"))["retrieved_passages"])
        assert docs == []


class TestRetrieveConfigFromState:
    """The ONLY config knob path is the state-seeded
    retrieval_config field — no execute(state, config=...) carve-out exists
    for this repo (the node signature takes no 2nd parameter)."""

    def test_ret_06_state_retrieval_config_kb_path_override_unreadable(self):
        state = _make_state(retrieval_config=to_json({"kb_path": "config/kb/does_not_exist.json"}))
        result = RetrieveNode()(state)
        assert from_json(result["retrieved_passages"]) == []
        notes = from_json(result.get("intake_notes"), [])
        assert any("not readable" in n for n in notes)

    def test_ret_07_state_retrieval_config_valid_kb_path_is_used(self):
        # An explicit (valid) kb_path in state must still resolve normally.
        state = _make_state(retrieval_config=to_json({"kb_path": "config/kb/fsa_mrm_kb.json"}))
        docs = from_json(RetrieveNode()(state)["retrieved_passages"])
        assert docs and docs[0]["id"] == "fsa-mrm-002"


class TestRetrieveNotesAccumulation:
    def test_ret_08_notes_append_never_clobber(self):
        state = _make_state(
            intake_notes=to_json(["earlier note from input validation"]),
            retrieval_config=to_json({"kb_path": "config/kb/bogus.json"}),
        )
        result = RetrieveNode()(state)
        notes = from_json(result["intake_notes"])
        assert notes[0] == "earlier note from input validation"
        assert len(notes) == 2
