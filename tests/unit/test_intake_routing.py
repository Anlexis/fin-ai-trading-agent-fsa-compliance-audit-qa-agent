# FIN-C2-196 — Unit Tests: the intake branch and its path-callable annotation
#
# Two separate things are proven here, and the first is easy to get wrong in a
# way that unit tests cannot see:
#
#   1. The path callable is annotated with THIS graph's State. The graph
#      library treats a path callable's annotation as its input schema and
#      projects away every field the annotation does not declare — so a
#      callable annotated with the framework's base state class cannot see any
#      domain field, its condition reads an always-absent value, and one branch
#      silently becomes unreachable. A direct call in a unit test does no
#      projection at all, so it passes either way; the annotation itself has to
#      be asserted, and the branch has to be proven through a real invoke.
#
#   2. A rejected request actually STOPS, and produces no answer of any kind.
#      (A node whose incoming state already carries an error status skips its
#      own execute(), so the refusal would hold even without the branch — but
#      that is a property of the node wrapper rather than a stated contract,
#      and this graph should not depend on it silently.)
#
# Deterministic — no model call, no network.

import typing

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.schemas.state import State, to_json


class TestPathCallableAnnotation:
    def test_route_after_intake_is_annotated_with_this_graphs_state(self):
        hints = typing.get_type_hints(DomainWorkflowGraph.route_after_intake)
        assert hints["state"] is State, (
            "a base-class annotation would hide every domain field from the "
            "path callable and make one branch unreachable"
        )

    def test_route_is_annotated_with_this_graphs_state(self):
        hints = typing.get_type_hints(DomainWorkflowGraph.route)
        assert hints["state"] is State

    def test_the_field_the_branch_reads_is_declared_on_that_state(self):
        # The projection only preserves declared fields, so the branch
        # condition must be one of them.
        assert "intake_rejected" in State.__annotations__

    def test_the_base_state_class_does_not_declare_it(self):
        # Which is what makes the annotation load-bearing rather than
        # decorative: annotate with the base class and this field is projected
        # away before the callable ever runs.
        from framework.schemas.agent_state import AgentState

        assert "intake_rejected" not in getattr(AgentState, "__annotations__", {})


class TestBranchDecisions:
    def test_accepted_intake_continues_to_retrieval(self):
        graph = DomainWorkflowGraph()
        assert graph.route_after_intake({"intake_rejected": False}) == "retrieve"

    def test_rejected_intake_ends_the_run(self):
        graph = DomainWorkflowGraph()
        assert graph.route_after_intake({"intake_rejected": True}) == END

    def test_error_status_ends_the_run(self):
        graph = DomainWorkflowGraph()
        assert graph.route_after_intake({"status": AgentStatus.ERROR.value}) == END


class TestBothBranchesAreReachableThroughTheCompiledGraph:
    """Proven through invoke(), not by calling the callable directly."""

    def _run(self, question, context):
        graph = DomainWorkflowGraph(
            config={
                "configurable": {
                    "retrieval": {"top_k": 4, "score_threshold": 0.25, "kb_path": "config/kb/fsa_mrm_kb.json"}
                }
            }
        )
        graph.compile()
        # Seed the caller channel the way the bridge does.
        graph._extra_initial_state = lambda: {  # type: ignore[method-assign]
            "retrieval_config": to_json({"top_k": 4, "score_threshold": 0.25, "kb_path": "config/kb/fsa_mrm_kb.json"}),
            "caller_context": to_json(context),
        }
        return graph.invoke(question)

    def test_accept_branch_produces_an_answer(self):
        out = self._run("what model validation records must be on file?", {})
        assert out["formatted_answer"]
        assert out["status"] == AgentStatus.SUCCESS.value

    def test_reject_branch_produces_no_answer_at_all(self):
        out = self._run("what model validation records must be on file?", {"top_k": 0})
        assert out["status"] == AgentStatus.ERROR.value
        assert not out.get("formatted_answer")
