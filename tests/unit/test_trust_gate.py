# FIN-C2-196 — Unit Tests: the trust gate and the credential output gate
#
# The main slot is ComplianceAuditGraphNode (src/graph/graph.py); this file
# covers the outer gate slots and the template's full trust matrix.
#
# Invocation canon: tests invoke nodes via node(state) — through
# BaseNode.__call__, which runs the trust gate, then the input gate, then
# execute(), then the output gate — never via node.execute(state) directly,
# which bypasses those gates. A denial RETURNS an error dict (never raises)
# with status ERROR and "trust gate denied" in error_log; execute() never
# runs, so execute-only output keys are ABSENT from the returned dict.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.generate_answer_node import GenerateAnswerNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.rerank_filter_node import RerankFilterNode
from src.nodes.retrieve_node import RetrieveNode


def _make_state(
    trust_value: str,
    user_input: str = "does this AI trading recommendation risk a definitive judgment under Article 38-2?",
    **extra,
) -> dict:
    state = {
        "user_input": user_input,
        "caller_trust_level": trust_value,
        "node_history": [],
        "error_log": [],
        "session_id": "test-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestTrustGate:
    """Trust gate tests — all invocations go through node(state) / __call__."""

    def test_anonymous_caller_allowed_on_inner_node(self):
        """An ANONYMOUS caller passes an ANONYMOUS inner domain node."""
        node = InputValidateNode()  # required_trust_level = ANONYMOUS
        result = node(_make_state(TrustLevel.ANONYMOUS.value))
        assert "trust gate denied" not in str(result.get("error_log", []))
        assert result.get("search_query"), "inner node should produce a normalised query"

    def test_anonymous_caller_denied_on_pre_process(self):
        """Rejection: ANONYMOUS caller on the VERIFIED_EXTERNAL PreProcessNode.

        __call__ must RETURN an error dict (never raise) with status ERROR and
        'trust gate denied' in the error_log. execute() never ran, so the
        execute-only output key (validated_input) must be ABSENT.
        """
        node = PreProcessNode()  # required_trust_level = VERIFIED_EXTERNAL
        result = node(_make_state(TrustLevel.ANONYMOUS.value))
        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            "trust gate denied" in str(e) for e in error_log
        ), f"Expected 'trust gate denied' in error_log, got: {error_log}"
        assert "validated_input" not in result, "execute() must not run on a trust denial — validated_input leaked"

    def test_verified_external_caller_passes_pre_process(self):
        """A VERIFIED_EXTERNAL caller clears the pre_process gate and
        the node writes the identifier-stripped validated_input."""
        node = PreProcessNode()
        result = node(_make_state(TrustLevel.VERIFIED_EXTERNAL.value))
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input")

    def test_pre_process_empty_input_rejected_after_gate(self):
        """The gate passes, then the node's own validation rejects empty input."""
        node = PreProcessNode()
        result = node(_make_state(TrustLevel.VERIFIED_EXTERNAL.value, user_input=""))
        assert result.get("status") == AgentStatus.ERROR.value
        assert any("empty" in str(e) for e in result.get("error_log", []))

    def test_anonymous_caller_denied_on_post_process(self):
        """Rejection on the other VERIFIED_EXTERNAL outer slot (post_process).

        The denial dict carries no execute-only key (formatted_output ABSENT).
        """
        node = PostProcessNode()  # required_trust_level = VERIFIED_EXTERNAL
        result = node(
            _make_state(
                TrustLevel.ANONYMOUS.value,
                result="a clean compliance analysis about record-retention duties",
            )
        )
        assert result.get("status") == AgentStatus.ERROR.value
        assert any("trust gate denied" in str(e) for e in result.get("error_log", []))
        assert "formatted_output" not in result, "execute() must not run on a trust denial — formatted_output leaked"

    def test_verified_external_caller_passes_post_process(self):
        """A VERIFIED_EXTERNAL caller clears the post_process gate."""
        node = PostProcessNode()
        result = node(
            _make_state(
                TrustLevel.VERIFIED_EXTERNAL.value,
                result="a clean compliance analysis about record-retention duties",
            )
        )
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("formatted_output")


class TestCredentialOutputGate:
    """The credential output gate — the post_process scan is real, not a no-op.

    Invoked through node(state) so the whole chain runs, same as production.
    """

    def test_credential_bearing_output_is_blocked(self):
        """A credential-shaped result is replaced by the sanitised stub.

        The token is assembled at runtime so no credential-shaped literal is
        stored in the repository.
        """
        leaked = "please use " + "sk-" + ("A" * 24) + " to authenticate"
        node = PostProcessNode()
        result = node(_make_state(TrustLevel.VERIFIED_EXTERNAL.value, result=leaked))
        assert result.get("status") == AgentStatus.ERROR.value
        assert "sk-" not in str(
            result.get("formatted_output", "")
        ), "the gate must not pass the credential through to formatted_output"
        assert any(
            "output blocked" in str(e) for e in result.get("error_log", [])
        ), f"Expected a gate block in error_log, got: {result.get('error_log')}"

    def test_clean_output_passes_the_gate(self):
        """A clean result is returned unchanged — the gate is a filter, not a wall."""
        clean = "Retain order records for seven years under the recordkeeping rules."
        node = PostProcessNode()
        result = node(_make_state(TrustLevel.VERIFIED_EXTERNAL.value, result=clean))
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("formatted_output") == clean


class TestTrustLevelMatrix:
    """The template's declared trust matrix (docs/02_design.md §security).

    Outer S-gate slots require VERIFIED_EXTERNAL (the manifest's
    required_trust_level); the five inner domain nodes run behind that outer
    boundary and are declared ANONYMOUS per the Cat-2 nested convention.
    """

    def test_outer_gate_nodes_require_verified_external(self):
        assert PreProcessNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL
        assert PostProcessNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_inner_domain_nodes_admit_anonymous(self):
        for node_cls in (
            InputValidateNode,
            RetrieveNode,
            RerankFilterNode,
            GenerateAnswerNode,
            OutputFormatNode,
        ):
            assert node_cls.required_trust_level is TrustLevel.ANONYMOUS, (
                f"{node_cls.__name__} must declare TrustLevel.ANONYMOUS " "(inner Cat-2 domain node)"
            )
