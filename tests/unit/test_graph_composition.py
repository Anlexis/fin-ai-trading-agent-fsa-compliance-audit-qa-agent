# FIN-C2-196 — Unit Tests: nested Cat-2 graph composition (outer + end-to-end)
#
# Drives the REAL outer agent (FSATradingComplianceAuditAgent / Graph)
# end-to-end via AgentBaseGraph.invoke(). The e2e context is
# InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) — the
# manifest's declared caller level; for_internal() is NEVER used (it would
# over-privilege the run and hide trust-gate regressions).
#
# Mirrors docs/03_test_spec.md §3 (INT-05..INT-12).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

import pathlib

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

import src.graph.graph
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import (
    ComplianceAuditGraphNode,
    FSATradingComplianceAuditAgent,
    Graph,
)
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json, to_json

_DEFINITIVE_QUERY = (
    "does this recommendation risk a definitive judgment under Article 38-2 " "- what should compliance check"
)


def _run(user_input: str, trust: TrustLevel = TrustLevel.VERIFIED_EXTERNAL) -> dict:
    ctx = InvocationContext(caller_trust_level=trust, caller_id="unit-suite")
    return Graph().invoke(user_input, ctx=ctx)


class TestOuterGraphConstruction:
    def test_int_05_inherits_agent_base_graph_directly(self):
        assert issubclass(FSATradingComplianceAuditAgent, AgentBaseGraph)

    def test_int_05_graph_alias(self):
        assert Graph is FSATradingComplianceAuditAgent

    def test_state_schema_is_state(self):
        assert FSATradingComplianceAuditAgent().state_schema is State

    def test_int_06_compile_fills_all_backbone_slots(self):
        agent = FSATradingComplianceAuditAgent()
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], ComplianceAuditGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_add_edges_is_not_overridden(self):
        # Backbone wiring belongs to the framework — the template must not
        # redefine it.
        assert "add_edges" not in FSATradingComplianceAuditAgent.__dict__


class TestMainSlotGraphNode:
    def test_int_07_get_subgraph_returns_the_inner_graph(self):
        subgraph = ComplianceAuditGraphNode().get_subgraph()
        assert isinstance(subgraph, DomainWorkflowGraph)
        assert subgraph.config["configurable"]["retrieval"], "inner config must carry the retrieval block"

    def test_int_08_extract_input_prefers_validated_input(self):
        node = ComplianceAuditGraphNode()
        assert node.extract_input({"validated_input": "VI", "user_input": "UI"}) == "VI"
        assert node.extract_input({"user_input": "UI"}) == "UI"

    def test_int_09_merge_output_maps_the_inner_contract(self):
        node = ComplianceAuditGraphNode()
        citations = to_json([{"ref": 1, "id": "fsa-mrm-001", "title": "t", "source": "s"}])
        checklist = to_json(["item"])
        delta = node.merge_output(
            {},
            {
                "formatted_answer": "ANSWER",
                "citations": citations,
                "classification_topic": "definitive_judgment",
                "remediation_checklist": checklist,
                "status": AgentStatus.SUCCESS.value,
            },
        )
        # The inner formatted_answer surfaces as BOTH compliance_answer and
        # result (PostProcessNode's gate reads state["result"]).
        assert delta == {
            "compliance_answer": "ANSWER",
            "result": "ANSWER",
            "citations": citations,
            "classification_topic": "definitive_judgment",
            "remediation_checklist": checklist,
            "status": AgentStatus.SUCCESS.value,
        }

    def test_error_strategy_is_propagate_and_hitl_is_contained(self):
        assert ComplianceAuditGraphNode.error_strategy == "propagate"
        assert ComplianceAuditGraphNode.propagate_hitl is False

    def test_int_10_parent_config_never_empty_without_runtime_config(self, monkeypatch):
        # With an unreadable config file the forwarded config still carries the
        # fallback retrieval block — never {}.
        monkeypatch.setattr(src.graph.graph, "_RUNTIME_CONFIG_PATH", pathlib.Path("/nonexistent/config.yaml"))
        cfg = ComplianceAuditGraphNode()._parent_config()
        assert cfg["configurable"]["retrieval"]["kb_path"] == "config/kb/fsa_mrm_kb.json"

    def test_parent_config_reads_the_runtime_file_not_the_manifest(self):
        # The flat manifest has no nested runtime block. A reader pointed at it
        # would return nothing and fall back silently, so every declared value
        # would be inert while still appearing configured.
        assert "config.yaml" in src.graph.graph._RUNTIME_CONFIG_PATH.name
        assert not hasattr(src.graph.graph, "_MANIFEST_PATH")


class TestOuterGetOutputStructuredFields:
    """The classification verdict + checklist ARE the product, surfaced as
    real decoded values on SUCCESS only, and re-scanned by the output gate a
    SECOND time (fail-closed)."""

    def test_non_success_state_gets_only_the_base_envelope(self):
        agent = FSATradingComplianceAuditAgent()
        base = agent.get_output({"status": AgentStatus.ERROR.value, "output": None})
        assert "classification_topic" not in base
        assert "remediation_checklist" not in base
        assert "citations" not in base

    def test_success_state_surfaces_decoded_structured_fields(self):
        agent = FSATradingComplianceAuditAgent()
        state = {
            "status": AgentStatus.SUCCESS.value,
            "result": "clean answer",
            "formatted_output": "clean answer",
            "classification_topic": "definitive_judgment",
            "remediation_checklist": to_json(["item one", "item two"]),
            "citations": to_json([{"ref": 1, "id": "fsa-mrm-001", "title": "t", "source": "s"}]),
            "node_history": ["x"],
        }
        out = agent.get_output(state)
        # Decoded Python values, not JSON strings.
        assert out["classification_topic"] == "definitive_judgment"
        assert out["remediation_checklist"] == ["item one", "item two"]
        assert out["citations"] == [{"ref": 1, "id": "fsa-mrm-001", "title": "t", "source": "s"}]

    def test_second_gate_pass_withholds_fields_on_a_violation(self):
        # A credential-shaped string surfacing through a structured field
        # (never through PostProcessNode's own scan of `result`) must still
        # be caught — the second, independent scan at the envelope
        # boundary.
        agent = FSATradingComplianceAuditAgent()
        secret = "sk-" + "D" * 24
        state = {
            "status": AgentStatus.SUCCESS.value,
            "result": "clean answer",
            "formatted_output": "clean answer",
            "classification_topic": "definitive_judgment",
            "remediation_checklist": to_json([f"use {secret} to authenticate"]),
            "citations": to_json([]),
            "node_history": ["x"],
        }
        out = agent.get_output(state)
        assert out["status"] == AgentStatus.ERROR.value
        assert "classification_topic" not in out
        assert "remediation_checklist" not in out
        assert secret not in str(out)


class TestEndToEndInvoke:
    """Full agent run: outer backbone + inner domain workflow, no LLM."""

    def test_int_11_invoke_returns_success(self):
        result = _run(_DEFINITIVE_QUERY)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_int_11_output_is_the_gated_formatted_answer(self):
        output = _run(_DEFINITIVE_QUERY).get("output")
        assert isinstance(output, str) and output.strip()
        assert output.startswith("# FSA Trading Compliance Audit Result")
        assert "[1]" in output
        assert "It is not legal advice, not a formal regulatory determination" in output

    def test_int_11_e2e_traverses_the_post_process_gate(self):
        history = _run(_DEFINITIVE_QUERY).get("node_history", [])
        for cls_name in ("PreProcessNode", "ComplianceAuditGraphNode", "PostProcessNode"):
            assert cls_name in history, f"node_history missing {cls_name}: {history}"

    def test_no_coverage_query_still_terminates_success(self):
        result = _run("quantum telepathy sandwich recipes")
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "does not contain sufficient coverage" in result.get("output", "")

    def test_int_12_anonymous_caller_is_denied_at_the_outer_boundary(self):
        """Trust gate at graph level: an ANONYMOUS invoke is refused by the
        VERIFIED_EXTERNAL pre_process slot. The error state short-circuits the
        main slot (its input gate sees status=error and skips the inner graph)
        and routes past post_process to finalize — no domain answer is ever
        produced."""
        result = _run(_DEFINITIVE_QUERY, trust=TrustLevel.ANONYMOUS)
        assert result.get("status") == AgentStatus.ERROR.value
        assert not result.get("output")
        history = result.get("node_history", [])
        assert "PostProcessNode" not in history
        assert history[:2] == ["InitializeNode", "PreProcessNode"]


class TestStateRoundTrip:
    """JSON-string helpers: producers to_json() on write, consumers from_json()."""

    def test_to_from_json_list_round_trip(self):
        original = [{"id": "fsa-mrm-001", "score": 0.6, "title": "definitive judgment"}]
        assert from_json(to_json(original)) == original

    def test_to_from_json_dict_round_trip(self):
        original = {"topic": "mrm_documentation", "top_k": 3}
        assert from_json(to_json(original)) == original

    def test_to_json_none_passes_through(self):
        assert to_json(None) is None

    def test_from_json_malformed_returns_default(self):
        assert from_json("{not valid json", default=[]) == []
        assert from_json(None, default={}) == {}
        assert from_json("", default=[]) == []
