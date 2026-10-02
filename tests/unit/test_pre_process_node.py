# FIN-C2-196 — Unit Tests: PreProcessNode (outer pre_process slot)
#
# Invocation canon: every test invokes the node via
# node(state) — BaseNode.__call__ → trust gate → input mask → execute()
# → credential gate — never a bare node.execute(state). PreProcessNode
# requires VERIFIED_EXTERNAL, so its behavioural tests build the state at that
# level (the ANONYMOUS rejection lives in test_trust_gate.py).
#
# Layering note: the FRAMEWORK input gate masks user_input /
# validated_input before execute() runs — e-mails, SSN/phone/CC digit groups
# and Title-Case name bigrams surface as [MASKED]. The NODE's own surface
# strip then catches IBAN-shaped tokens the framework patterns do not, and
# replaces them with [REDACTED]. Intentional-PII tests therefore assert the
# raw identifier is GONE and the corresponding masked marker is present.
# Verified empirically against the installed shared.security.pii_detector:
# "Article 38-2" and "AI trading" do NOT trip the Title-Case name bigram scan
# (the pattern requires TWO consecutive `[A-Z][a-z]+`-shaped words — "AI" and
# a trailing digit token do not qualify), so the positive-path query below
# stays clean end to end.
#
# Mirrors docs/03_test_spec.md §2.1 (PRE-01..PRE-08).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from unittest.mock import MagicMock

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

import src.nodes.pre_process_node
from src.nodes.pre_process_node import PreProcessNode

# identifier-free (no Title-Case bigram, no @, no digit run) — the framework mask leaves
# the payload untouched, so this is a clean positive-path fixture.
_VALID_QUERY = "does this AI trading recommendation risk a definitive judgment under " "Article 38-2?"


def _make_state(user_input=_VALID_QUERY, **extra) -> dict:
    state = {
        "user_input": user_input,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPreProcessSuccess:
    def test_pre_01_valid_query_accepted(self):
        result = PreProcessNode()(_make_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain string, never the enum.
        assert isinstance(result["status"], str)
        assert result["validated_input"] == _VALID_QUERY

    def test_enriched_context_carries_channel(self):
        result = PreProcessNode()(_make_state(input_context={"channel": "web"}))
        assert result["enriched_context"]["channel"] == "web"
        assert result["enriched_context"]["source"] == "FSATradingComplianceAuditAgent"

    def test_missing_channel_defaults_to_unknown(self):
        result = PreProcessNode()(_make_state())
        assert result["enriched_context"]["channel"] == "unknown"


class TestPreProcessRejection:
    def test_pre_02_empty_input_is_error(self):
        result = PreProcessNode()(_make_state(user_input=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
        # No validated_input is produced on the reject path.
        assert "validated_input" not in result

    def test_whitespace_only_is_error(self):
        result = PreProcessNode()(_make_state(user_input="   \n\t "))
        assert result["status"] == AgentStatus.ERROR.value

    def test_pre_03_missing_user_input_is_error(self):
        state = _make_state()
        del state["user_input"]
        result = PreProcessNode()(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_non_string_input_is_error(self):
        result = PreProcessNode()(_make_state(user_input={"malicious": "dict"}))
        assert result["status"] == AgentStatus.ERROR.value


class TestPreProcessIdentifierScreen:
    """PRE-04: raw identifiers never survive into validated_input."""

    def test_iban_redacted_by_node_screen(self):
        # IBAN-shaped tokens are NOT in the framework's own patterns — the
        # node's own surface strip must catch them ([REDACTED] path).
        raw = "verify onboarding checks for account DE89370400440532013000 before activation"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "DE89370400440532013000" not in vi
        assert "[REDACTED]" in vi

    def test_email_masked_by_framework_input_gate(self):
        # The framework input gate masks e-mail before execute() sees it.
        raw = "escalate the mrm review to compliance.desk@example.com today"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "compliance.desk@example.com" not in vi
        assert "[MASKED]" in vi

    def test_grouped_account_digits_masked(self):
        # 4-4-4 digit groups match the framework's number patterns.
        raw = "account 1234 5678 9012 shows a pending verification flag"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "1234 5678 9012" not in vi
        assert "[MASKED]" in vi

    def test_title_case_bigram_masked_by_framework_input_gate(self):
        # Regulatory Title-Case bigrams DO trip the framework name scan
        # — e.g. "Bank Act". Verified against the real
        # detector: two consecutive [A-Z][a-z]+-shaped words match.
        raw = "confirm this disclosure satisfies the Bank Act before it goes out"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "Bank Act" not in vi
        assert "[MASKED]" in vi


class TestPreProcessAudit:
    def test_pre_08_domain_audit_payload(self, monkeypatch):
        """The accepted request emits pre_process_complete; the assertion
        targets call.args[1] — the event payload — never the whole call repr."""
        spy = MagicMock()
        monkeypatch.setattr(src.nodes.pre_process_node, "emit_trace_event", spy)
        PreProcessNode()(_make_state())
        events = [call.args[0] for call in spy.call_args_list]
        assert "pre_process_complete" in events
        payload = spy.call_args_list[events.index("pre_process_complete")].args[1]
        assert payload["input_chars"] == len(_VALID_QUERY)

    def test_pre_process_rejected_event_on_empty_input(self, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr(src.nodes.pre_process_node, "emit_trace_event", spy)
        PreProcessNode()(_make_state(user_input=""))
        events = [call.args[0] for call in spy.call_args_list]
        assert "pre_process_rejected" in events
        payload = spy.call_args_list[events.index("pre_process_rejected")].args[1]
        assert payload["reason"] == "empty_input"
