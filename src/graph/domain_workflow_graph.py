"""AgentCore Platform v1.0"""

# FIN-C2-196 - DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full FSA trading-compliance audit domain workflow:
#
#   START -> input_validate -> retrieve -> rerank_filter
#         -> generate_answer -> output_format -> END
#
# Called by ComplianceAuditGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   - Inherits BaseGraph (fully custom topology - no forced backbone)
#   - Implements all 7 BaseGraph ABC methods
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - register_nodes() instantiates every domain node with NO ctor args
#   - Does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() designed together with ComplianceAuditGraphNode.merge_output()
#   - No platform SDK imports

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.generate_answer_node import GenerateAnswerNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.rerank_filter_node import RerankFilterNode
from src.graph.context_bridge import take_caller_context
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-196.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ComplianceAuditGraphNode.get_subgraph() in graph.py, which
    passes the manifest-derived config (`_parent_config()`) into the ctor.

    Pipeline (linear):
        START
          -> input_validate  (InputValidateNode)  - parse, normalise, classify
          -> retrieve        (RetrieveNode)       - keyword-score the seeded FSA-MRM KB
          -> rerank_filter   (RerankFilterNode)   - boost / threshold / top_k cut
          -> generate_answer (GenerateAnswerNode) - grounded analysis + citations + checklist
          -> output_format   (OutputFormatNode)   - final format + disclaimer + block-definitive gate
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns - not registered here.
    """

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "fin_c2_196_fsa_trading_compliance_audit_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The forwarded `retrieval` block (top_k / score_threshold / kb_path) is
        read per-call by the domain nodes with safe defaults, so absence is
        non-fatal. Validation is permissive here rather than raising
        ConfigError.
        """
        pass

    # -- Config forwarding into state (manifest -> inner nodes) -----------------

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner state with the runtime config and the caller context.

        Two values reach the domain nodes here:

        `retrieval_config` - ComplianceAuditGraphNode._parent_config() forwards
        the runtime `retrieval` block under config["configurable"]; this hook
        makes it reachable by the domain nodes at runtime as a JSON-string
        state field (a JSON string rather than a bare dict, so the field stays
        safe to checkpoint). RetrieveNode / RerankFilterNode read that state
        field; execute() takes no `config` parameter.

        `caller_context` - the structured caller channel. The framework's
        GraphNode invokes this graph with the string input only, so the outer
        node stashed the context on the bridge in extract_input(); this is
        where it is picked up and seeded. The value is UNVALIDATED caller data
        and is validated by InputValidateNode, the intake node, before any
        other node reads it.
        """
        retrieval = (self.config or {}).get("configurable", {}).get("retrieval") or {}
        return {
            "retrieval_config": to_json(retrieval),
            "caller_context": to_json(take_caller_context()),
        }

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call - BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.

        Every node is instantiated with NO constructor arguments - SDK v1.0.0rc1
        FunctionNode subclasses take no __init__; config flows in via the
        state-seeded `retrieval_config` field (_extra_initial_state() above),
        never a per-call execute() parameter.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["retrieve"] = RetrieveNode()
        self._nodes["rerank_filter"] = RerankFilterNode()
        self._nodes["generate_answer"] = GenerateAnswerNode()
        self._nodes["output_format"] = OutputFormatNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the compliance-audit domain topology.

        The pipeline is linear apart from one branch: intake either accepts the
        request and the audit proceeds, or it rejects the request and the run
        ends immediately.

        The branch is an explicit edge rather than a reliance on the framework.
        A node whose incoming state already carries an error status does skip
        its own execute(), so a refused request would produce no answer even
        without this edge - but that is an implementation detail of the node
        wrapper, not a stated contract, and depending on it silently would mean
        walking four more nodes for a request that was already refused and
        emitting a lifecycle event from each. Routing to END states the
        intention where a reader can see it, and keeps the refusal fail-closed
        on this graph's own terms.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_conditional_edges(
            "input_validate",
            self.route_after_intake,
            {"retrieve": "retrieve", END: END},
        )
        self._sg.add_edge("retrieve", "rerank_filter")
        self._sg.add_edge("rerank_filter", "generate_answer")
        self._sg.add_edge("generate_answer", "output_format")
        self._sg.add_edge("output_format", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: State) -> str:
        """Conditional routing - required by BaseGraph ABC.

        The graph's own branch is wired through route_after_intake(); this
        method satisfies the ABC contract and returns END on error so an
        unexpected call can never re-enter a processing node.

        The annotation is this graph's own State, deliberately. The graph
        library reads a path callable's annotation as that callable's input
        schema and PROJECTS AWAY every field the annotation does not declare.
        Annotating a path callable with the framework's base state class
        therefore hides every domain field from it - the branch condition then
        reads an always-absent value and one edge silently becomes unreachable,
        while unit tests that call the method directly keep passing because a
        direct call does no projection at all.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    def route_after_intake(self, state: State) -> str:
        """Continue to retrieval, or end the run when intake rejected the request.

        Annotated with this graph's own State for the projection reason set out
        in route() above. `intake_rejected` is a domain field: measured against
        the installed library, a callable annotated with the framework's base
        state class is handed 16 keys and this one is not among them, while the
        same callable annotated with State is handed 19 and it is. Both
        conditions are kept deliberately - the domain flag says what happened,
        the status check is the belt to its braces.
        """
        if state.get("intake_rejected") or state.get("status") == AgentStatus.ERROR.value:
            return END
        return "retrieve"

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ComplianceAuditGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()  emits: "formatted_answer", "citations",
                                        "classification_topic",
                                        "remediation_checklist", "status", ...
            Outer merge_output() reads: sub_result.get("formatted_answer"),
                                        sub_result.get("citations"),
                                        sub_result.get("classification_topic"),
                                        sub_result.get("remediation_checklist"),
                                        sub_result.get("status")

        Additional fields (intake_notes, trace_id, correlation_id,
        node_history) are surfaced for observability / downstream extension.
        """
        return {
            "formatted_answer": state.get("formatted_answer"),
            "citations": state.get("citations"),
            "classification_topic": state.get("classification_topic"),
            "remediation_checklist": state.get("remediation_checklist"),
            "status": state.get("status"),
            "intake_notes": state.get("intake_notes"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
