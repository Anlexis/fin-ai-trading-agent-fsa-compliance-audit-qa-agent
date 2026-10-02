# PB-CONTAIN — a violating output gate must CLEAR the output, not just relabel it.
#
# The framework builds the caller-facing envelope as
# `{"output": state["formatted_output"] or state["result"], "status": ..., ...}`
# and it does that REGARDLESS of the status value. So an output gate that
# detects a violation and merely flips the status to error still hands the
# caller the un-gated answer text — inside an envelope labelled "error". The
# label changes; the content ships.
#
# The outer graph's get_output() runs a second content scan over the structured
# fields, which are read straight from state and were never part of `result`,
# so this is a reachable path rather than a theoretical one: a knowledge-base
# entry whose identifier carries credential-shaped text reaches the citations
# without ever appearing in the rendered answer the first gate inspects.
#
# These tests pin the containment behaviour against the installed framework
# wheel, not against our own assumptions about it.
#
# Deterministic — no model call, no network.

import json

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import FSATradingComplianceAuditAgent

_RELEASED_TEXT = "# FSA Trading Compliance Audit Result\nfull rendered analysis body"
_CREDENTIAL = "sk-livecredential0123456789abcdef"


def _violating_state() -> dict:
    """A SUCCESS state whose structured fields carry credential-shaped content.

    `result` holds the rendered answer, exactly as it would after a clean run;
    the violation lives only in a field the first gate never inspected.
    """
    return {
        "status": AgentStatus.SUCCESS.value,
        "formatted_output": _RELEASED_TEXT,
        "result": _RELEASED_TEXT,
        "trace_id": "trace-1",
        "correlation_id": "corr-1",
        "node_history": ["pre_process", "main", "post_process"],
        "classification_topic": "mrm_documentation",
        "remediation_checklist": json.dumps(["confirm the model card exists"]),
        "citations": json.dumps([{"ref": 1, "id": _CREDENTIAL, "title": "t", "source": "s"}]),
    }


class TestFrameworkEnvelopeAssumption:
    """The premise the containment fix rests on, asserted against the wheel."""

    def test_base_envelope_carries_the_answer_even_on_error_status(self):
        agent = FSATradingComplianceAuditAgent()
        base = super(FSATradingComplianceAuditAgent, agent).get_output(
            {"status": AgentStatus.ERROR.value, "result": _RELEASED_TEXT}
        )
        # This is WHY clearing is required: an error status alone does not
        # stop the framework from publishing the answer.
        assert base["output"] == _RELEASED_TEXT


class TestViolationIsContained:
    def test_violation_returns_error_status(self):
        out = FSATradingComplianceAuditAgent().get_output(_violating_state())
        assert out["status"] == AgentStatus.ERROR.value

    def test_violation_releases_no_answer_text(self):
        out = FSATradingComplianceAuditAgent().get_output(_violating_state())
        assert out["output"] is None
        assert _RELEASED_TEXT not in json.dumps(out)

    def test_violation_releases_no_structured_fields(self):
        out = FSATradingComplianceAuditAgent().get_output(_violating_state())
        for key in ("classification_topic", "remediation_checklist", "citations"):
            assert key not in out

    def test_violation_releases_no_credential_material(self):
        out = FSATradingComplianceAuditAgent().get_output(_violating_state())
        assert _CREDENTIAL not in json.dumps(out)

    def test_error_envelope_carries_no_traceback_or_source_paths(self):
        rendered = json.dumps(FSATradingComplianceAuditAgent().get_output(_violating_state()))
        assert "Traceback" not in rendered
        assert ".py" not in rendered
        assert "/src/" not in rendered

    def test_correlation_keys_survive_so_the_request_stays_traceable(self):
        out = FSATradingComplianceAuditAgent().get_output(_violating_state())
        assert out["trace_id"] == "trace-1"
        assert out["correlation_id"] == "corr-1"


class TestCleanRunIsUnaffected:
    def test_clean_state_surfaces_the_answer_and_the_structured_fields(self):
        state = _violating_state()
        state["citations"] = json.dumps([{"ref": 1, "id": "fsa-mrm-004", "title": "t", "source": "s"}])
        out = FSATradingComplianceAuditAgent().get_output(state)
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["output"] == _RELEASED_TEXT
        assert out["classification_topic"] == "mrm_documentation"
        assert out["citations"]
