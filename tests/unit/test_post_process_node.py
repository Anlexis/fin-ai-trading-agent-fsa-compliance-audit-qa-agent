# FIN-C2-196 — Unit Tests: PostProcessNode (outer post_process slot)
#
# Invocation canon: node(state) via BaseNode.__call__. PostProcessNode is the
# second outer gate slot and requires VERIFIED_EXTERNAL (like PreProcessNode),
# so its behavioural tests build the state at that level; the ANONYMOUS
# rejection lives in test_trust_gate.py.
#
# Layering: the node's own module-level _security_gate_output() scan runs
# INSIDE execute() and replaces a violating answer with the sanitised stub
# (returned dict — no exception). The framework's own credential scan then
# sees only the clean stub. Intentional-credential tests assert the raw
# secret never survives into formatted_output OR result.
#
# Mirrors docs/03_test_spec.md §2.7 (POST-01..POST-06).
# Deterministic — no model call, no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _security_gate_output

_CLEAN_REPORT = (
    "# FSA Trading Compliance Audit Result\n\n"
    "[1] a compliance officer should confirm the review step was followed.\n"
)

# JWT-shaped token built at runtime so no credential-shaped literal ever sits
# in the repository (credential-scan hygiene).
_FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def _make_state(result_text, **extra) -> dict:
    state = {
        "result": result_text,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPostProcessClean:
    def test_post_01_clean_output_passes_through(self):
        result = PostProcessNode()(_make_state(_CLEAN_REPORT))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain string, never the enum.
        assert isinstance(result["status"], str)
        assert result["formatted_output"] == _CLEAN_REPORT

    def test_post_02_empty_result_is_non_fatal(self):
        result = PostProcessNode()(_make_state(""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == ""

    def test_post_02_missing_result_key_is_non_fatal(self):
        state = _make_state("")
        del state["result"]
        result = PostProcessNode()(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestPostProcessS3Gate:
    def _assert_blocked(self, result, secret):
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output blocked" in str(e) for e in result["error_log"])
        # The raw secret must not survive into either surfaced field.
        assert secret not in str(result.get("formatted_output", ""))
        assert secret not in str(result.get("result", ""))
        assert "[OUTPUT BLOCKED by the credential gate" in result["formatted_output"]

    def test_post_03_api_key_is_blocked(self):
        secret = "sk-ABCDEF0123456789abcdef"
        result = PostProcessNode()(_make_state(f"# Report\n\n<!-- debug api_key={secret} -->\n"))
        self._assert_blocked(result, secret)

    def test_post_04_credential_assignment_is_blocked(self):
        secret = "password=super_secret_value_123"
        result = PostProcessNode()(_make_state(f"# Report\n\ninternal note: {secret}\n"))
        self._assert_blocked(result, "super_secret_value_123")

    def test_post_05_jwt_is_blocked(self):
        result = PostProcessNode()(_make_state(f"# Report\n\nsession token {_FAKE_JWT}\n"))
        self._assert_blocked(result, _FAKE_JWT)

    def test_post_06_bearer_token_is_blocked(self):
        secret = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
        result = PostProcessNode()(_make_state(f"# Report\n\nauthorization: {secret}\n"))
        self._assert_blocked(result, secret)


class TestSecurityGateOutputRecursion:
    """The credential scan walks nested dict/list structures
    recursively (a credential nested one level down must not slip through)."""

    def test_scan_finds_credential_nested_in_a_dict(self):
        secret = "sk-" + "B" * 24
        violation = _security_gate_output({"outer": {"inner": f"use {secret} to authenticate"}})
        assert violation == "api_key"

    def test_scan_finds_credential_nested_in_a_list(self):
        secret = "sk-" + "C" * 24
        violation = _security_gate_output(["clean value", {"note": secret}])
        assert violation == "api_key"

    def test_scan_is_clean_for_non_string_scalars(self):
        assert _security_gate_output({"count": 3, "ok": True, "nothing": None}) is None
