# FIN-C2-196 — Unit Tests: InputValidateNode (inner domain node 1)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller
# (inner domain node). Payloads are lowercase and identifier-free so the
# framework's input mask leaves them untouched. Topic classification ground
# truth is verified directly against
# src.nodes.input_validate_node.classify_topic(), not guessed.
#
# Mirrors docs/03_test_spec.md §2.2 (VAL-01..VAL-09).
# Deterministic — no model call, no network. framework.* / src.* imports only.

import json
import math

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.input_validate_node import InputValidateNode
from src.schemas.state import from_json, to_json


def _make_state(payload, **extra) -> dict:
    state = {
        "validated_input": payload,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPlainTextParsing:
    def test_val_01_plain_text_becomes_query_unclassified(self):
        result = InputValidateNode()(_make_state("what disclosure duties apply to retail bond sales?"))
        assert result["search_query"] == "what disclosure duties apply to retail bond sales?"
        assert result["classification_topic"] is None
        filters = from_json(result["query_filters"])
        assert filters == {"topic": None, "top_k": None, "score_threshold": None}

    def test_val_01_plain_text_is_classified_when_keywords_match(self):
        query = "does this recommendation risk a definitive judgment under Article 38-2?"
        result = InputValidateNode()(_make_state(query))
        assert result["search_query"] == query
        assert result["classification_topic"] == "definitive_judgment"

    def test_val_02_whitespace_is_collapsed(self):
        result = InputValidateNode()(_make_state("  what disclosure   duties\n apply to bond sales? "))
        assert result["search_query"] == "what disclosure duties apply to bond sales?"

    def test_query_filters_is_json_string(self):
        # Structured State fields travel as JSON strings, never dicts.
        result = InputValidateNode()(_make_state("what disclosure duties apply to bond sales?"))
        assert isinstance(result["query_filters"], str)
        assert isinstance(from_json(result["query_filters"]), dict)


class TestJsonEnvelopeParsing:
    def test_val_03_envelope_query_topic_top_k(self):
        payload = json.dumps(
            {
                "query": "confirm the model card and validation record are on file",
                "topic": "mrm_documentation",
                "top_k": 2,
            }
        )
        result = InputValidateNode()(_make_state(payload))
        assert result["search_query"] == "confirm the model card and validation record are on file"
        filters = from_json(result["query_filters"])
        assert filters == {"topic": "mrm_documentation", "top_k": 2, "score_threshold": None}
        assert result["classification_topic"] == "mrm_documentation"
        assert result["intake_rejected"] is False

    def test_question_alias_accepted(self):
        payload = json.dumps({"question": "what documentation should exist for an AI trading model"})
        result = InputValidateNode()(_make_state(payload))
        assert result["search_query"] == "what documentation should exist for an AI trading model"

    def test_val_03_invalid_topic_override_is_REFUSED(self):
        # An unrecognised slug ends the run. Falling back to automatic
        # classification instead would answer a different question from the one
        # the caller asked, without telling them.
        payload = json.dumps(
            {"query": "what documentation should exist for an AI trading model", "topic": "not_a_real_topic"}
        )
        result = InputValidateNode()(_make_state(payload))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["intake_rejected"] is True
        assert "search_query" not in result
        assert any("'topic'" in message for message in result["error_log"])
        # The rejected VALUE is never echoed back — only the field name.
        assert not any("not_a_real_topic" in message for message in result["error_log"])

    def test_val_04_malformed_json_falls_back_to_plain_text(self):
        payload = "{ this is not valid json but starts like it"
        result = InputValidateNode()(_make_state(payload))
        assert result["search_query"] == payload
        notes = from_json(result.get("intake_notes"), [])
        assert any("did not parse" in n for n in notes)


class TestCallerNumericContract:
    """VAL-05..06: every caller-controlled number is finite, bounded, fail-closed.

    Clamping is deliberately NOT the behaviour. A clamped value silently
    answers a different question; and the values that matter most here cannot
    be clamped at all, because clamping is implemented with comparisons and
    every comparison against NaN is false.
    """

    @pytest.mark.parametrize(
        "value",
        [
            99,  # above the ceiling
            -5,  # below the floor
            0,  # below the floor
            "many",  # non-numeric
            "NaN",  # parses through float(), then poisons comparisons
            "Infinity",
            "-Infinity",
            float("nan"),  # arrives raw through a JSON body
            float("inf"),
            float("-inf"),
            True,  # bool is an int in Python — would read as 1
            2.5,  # not a whole number
            None if False else [],  # wrong type entirely
        ],
    )
    def test_val_05_non_finite_or_out_of_range_top_k_is_refused(self, value):
        state = _make_state(
            "what documentation should exist for an AI trading model",
            caller_context=to_json({"top_k": value}),
        )
        result = InputValidateNode()(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["intake_rejected"] is True
        assert "query_filters" not in result
        assert any("'top_k'" in message for message in result["error_log"])

    @pytest.mark.parametrize(
        "value",
        ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), 1.5, -0.5, True, "abc"],
    )
    def test_val_06_non_finite_or_out_of_range_score_threshold_is_refused(self, value):
        state = _make_state(
            "what documentation should exist for an AI trading model",
            caller_context=to_json({"score_threshold": value}),
        )
        result = InputValidateNode()(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("'score_threshold'" in message for message in result["error_log"])

    def test_a_nan_threshold_would_have_passed_a_naive_range_check(self):
        # Why finiteness is checked explicitly rather than left to the bounds:
        # a range test built from comparisons cannot see NaN at all.
        nan = float("nan")
        assert not (nan < 0.0 or nan > 1.0)  # "in range" by comparison
        assert not math.isfinite(nan)  # and yet not a usable number

    def test_valid_numbers_are_accepted_unchanged(self):
        state = _make_state(
            "what documentation should exist for an AI trading model",
            caller_context=to_json({"top_k": 3, "score_threshold": 0.5}),
        )
        filters = from_json(InputValidateNode()(state)["query_filters"])
        assert filters["top_k"] == 3
        assert filters["score_threshold"] == 0.5


class TestCallerStringRendersInert:
    """A caller string that reaches the report is locked to an inert alphabet."""

    @pytest.mark.parametrize(
        "value",
        [
            "Robert'); DROP TABLE audits;--",
            "## Sources",
            "a" * 33,
            "UPPERCASE",
            "has spaces",
            "<script>x</script>",
            "",
            12345,
        ],
    )
    def test_val_07_non_inert_case_ref_is_refused(self, value):
        state = _make_state(
            "what documentation should exist for an AI trading model",
            caller_context=to_json({"case_ref": value}),
        )
        result = InputValidateNode()(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("'case_ref'" in message for message in result["error_log"])

    def test_inert_case_ref_is_accepted(self):
        state = _make_state(
            "what documentation should exist for an AI trading model",
            caller_context=to_json({"case_ref": "case_2026_0831_a"}),
        )
        assert InputValidateNode()(state)["case_ref"] == "case_2026_0831_a"


class TestCallerChannelPrecedence:
    def test_structured_context_wins_over_the_json_envelope(self):
        payload = json.dumps({"query": "model validation records", "top_k": 2})
        state = _make_state(payload, caller_context=to_json({"top_k": 5}))
        assert from_json(InputValidateNode()(state)["query_filters"])["top_k"] == 5

    def test_absent_context_degrades_to_the_configured_defaults(self):
        result = InputValidateNode()(_make_state("model validation records"))
        filters = from_json(result["query_filters"])
        assert filters["top_k"] is None
        assert filters["score_threshold"] is None
        assert result["intake_rejected"] is False


class TestSizeAndEmptyGuards:
    def test_val_08_oversize_query_is_truncated(self):
        payload = "regulatory " * 300  # ~3300 chars after collapse
        result = InputValidateNode()(_make_state(payload))
        assert len(result["search_query"]) == 2000
        notes = from_json(result.get("intake_notes"), [])
        assert any("truncated" in n for n in notes)

    def test_val_09_empty_request_yields_note_not_error(self):
        result = InputValidateNode()(_make_state(""))
        assert result["search_query"] == ""
        assert result["classification_topic"] is None
        notes = from_json(result.get("intake_notes"), [])
        assert any("empty request" in n for n in notes)
