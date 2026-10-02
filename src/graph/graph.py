"""AgentCore Platform v1.0"""

# FIN-C2-196 - Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# FSA Trading Compliance Audit Agent (Cat 2 RAG domain workflow).
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed - identical to Cat 1, do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (RETRY, max 3)
#                                             -> pre_process
#
#   `main` slot is a GraphNode subclass (ComplianceAuditGraphNode) that
#   delegates the full compliance-audit domain workflow to DomainWorkflowGraph
#   (inner BaseGraph: input_validate -> retrieve -> rerank_filter ->
#   generate_answer -> output_format).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#
# Class-name contract:
#   graph.py class:           FSATradingComplianceAuditAgent (this file)
#   config/agent.yaml class:  "FSATradingComplianceAuditAgent"  <- must match
#   src/api/server.py import: from src.graph.graph import FSATradingComplianceAuditAgent
#
# Rules enforced:
#   - FSATradingComplianceAuditAgent inherits AgentBaseGraph directly
#   - super().register_nodes() called first (fills initialize + finalize)
#   - ComplianceAuditGraphNode assigned to self._nodes["main"]
#   - _parent_config() forwards the runtime retrieval config (never {})
#   - merge_output() returns only changed keys
#   - get_output() EXTENDS super().get_output() - structured keys surfaced
#     ONLY on SUCCESS, and fail-closed: a violation clears the output
#   - add_edges() NOT overridden on the outer graph
#   - No platform SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import stash_caller_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json


# The Marketplace runner seeds input_context with its own conversation history on every
# invocation (shared/bootstrap/marketplace_app.py); the caller neither sends that key nor can
# suppress it, and the build_input_context hook can only overwrite its value, never remove it.
# It is platform plumbing rather than caller data, so it is dropped here, before the caller
# contract runs: the unknown-field guard below stays strict for everything a caller can
# actually send, and no value screen is ever asked to judge a transcript that contains this
# agent's own earlier answers. The value may also be None, which this tolerates.
_PLATFORM_CONTEXT_KEYS = frozenset({"conversation_history"})


def _without_platform_context(raw: Any) -> Any:
    """The caller-supplied half of input_context, platform-injected keys removed."""
    if not isinstance(raw, dict):
        return raw
    return {k: v for k, v in raw.items() if k not in _PLATFORM_CONTEXT_KEYS}


# Runtime config path: src/graph/graph.py -> parents[2] = repo root.
#
# The tuning values live in config/config.yaml, NOT in config/agent.yaml.
# config/agent.yaml is the flat static manifest the registry reads for
# discovery; it has no nested runtime block, so reading tuning values from it
# would return nothing and every declared value would silently be replaced by
# the fallback below - a dead declaration that still looks configured.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Fallback mirrors the `retrieval` block in config/config.yaml so
# _parent_config() never forwards an empty config even if that file is
# unreadable in an exotic deployment layout.
_FALLBACK_RETRIEVAL = {
    "top_k": 4,
    "score_threshold": 0.25,
    "kb_path": "config/kb/fsa_mrm_kb.json",
}


class ComplianceAuditGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the outer agent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph compliance-audit pipeline).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    - instantiate DomainWorkflowGraph with the forwarded
                          manifest config (_parent_config())
      extract_input()   - pull validated_input (identifier-stripped) from outer state
      merge_output()    - map sub_result fields into outer state delta (changed keys only)
      error_strategy    - "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # S-1 declared on the wrapper too: the CI gate only AST-scans FunctionNode
    # subclasses, so a GraphNode main slot passes the pipeline without one and is
    # flagged at review. Same level the nodes in this repo already declare.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # "propagate": re-raise inner graph exceptions as SubgraphError (default - fail fast).
    # "handle": call on_subgraph_error() instead - use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only. This
    # template does not use HITL at all (docs/02_design.md - hitl.enabled
    # stays false).
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the runtime `retrieval` block to the inner graph.

        Loads config/config.yaml and returns the tuning block under
        config["configurable"] - never an empty dict. The inner graph
        republishes it into inner state
        (DomainWorkflowGraph._extra_initial_state()) so RetrieveNode /
        RerankFilterNode read the live top_k / score_threshold values.
        """
        runtime: Dict[str, Any] = {}
        try:
            import yaml

            loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                runtime = loaded
        except Exception:
            runtime = {}
        retrieval = runtime.get("retrieval")
        if not isinstance(retrieval, dict) or not retrieval:
            retrieval = dict(_FALLBACK_RETRIEVAL)
        return {"configurable": {"retrieval": retrieval}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time.

        The inner graph receives the runtime config via its BaseGraph
        ctor (graph-level constructor injection of immutable config - distinct
        from the per-node execute() contract); its domain NODES still take no
        constructor arguments and read config exclusively via the
        state-seeded `retrieval_config` field (execute(self, state) -> dict,
        no config parameter).
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates, screens and identifier-strips the raw
        user_input and writes the result to validated_input. Prefer that; fall
        back to user_input if validated_input is absent (e.g. in unit tests).

        This is also where the structured caller context crosses into the inner
        graph. The framework's GraphNode invokes the subgraph with the string
        input only - it passes no input_context - so the context is stashed on
        the bridge here and read back by the inner graph when it seeds its
        initial state. The value stashed is unvalidated caller data; the inner
        intake node validates it.
        """
        stash_caller_context(_without_platform_context(state.get("input_context", {})))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys - never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "formatted_answer", "citations",
                                        "classification_topic",
                                        "remediation_checklist", "status", ...
          This merge_output() reads -> sub_result.get("formatted_answer"),
                                       sub_result.get("citations"),
                                       sub_result.get("classification_topic"),
                                       sub_result.get("remediation_checklist"),
                                       sub_result.get("status")

        compliance_answer (str | None): final rendered compliance answer;
          written by OutputFormatNode inside the inner graph.
        result: PostProcessNode (outer post_process slot) reads
          state.get("result") - the inner graph emits the rendered answer
          under "formatted_answer", so map it to "result" as well; otherwise
          the final output surfaced by PostProcessNode (and its gate) is
          always empty.
        classification_topic / remediation_checklist: re-surfaced at the
          outer layer (still JSON strings, for checkpoint safety) so
          FSATradingComplianceAuditAgent.get_output() can read them without
          reaching into the inner graph.
        status (str | None): terminal AgentStatus value from the inner graph run.
        """
        return {
            "compliance_answer": sub_result.get("formatted_answer"),
            "result": sub_result.get("formatted_answer"),
            "citations": sub_result.get("citations"),
            "classification_topic": sub_result.get("classification_topic"),
            "remediation_checklist": sub_result.get("remediation_checklist"),
            "status": sub_result.get("status"),
        }


class FSATradingComplianceAuditAgent(AgentBaseGraph):
    """Outer graph for FIN-C2-196 (Cat 2 RAG, nested).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ComplianceAuditGraphNode (main slot), which delegates
    to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed - identical to Cat 1):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() and get_output() are the ONLY overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (input validation, screening, redaction)
      - main:         ComplianceAuditGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (credential output gate)
      - get_output():  extends the base envelope with the structured
        classification_topic / remediation_checklist / citations fields,
        ONLY on SUCCESS, fail-closed

    add_edges() is NOT overridden - backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "FSATradingComplianceAuditAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first - it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ComplianceAuditGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Extend the base envelope with the structured product fields.

        The classification verdict and the documentation/remediation checklist
        ARE the product, not a side note, so they are surfaced as real
        (decoded) Python values on top of the base
        `output`/`status`/`trace_id`/`correlation_id`/`node_history` envelope
        from AgentBaseGraph.get_output().

        Fail-closed, twice over:
          1. Only when the terminal state.status is SUCCESS are the structured
             fields attached at all - a non-SUCCESS run (trust denial, blocked
             output, subgraph error, ...) returns only the base envelope.
          2. Even on SUCCESS, the structured fields are re-scanned through the
             same `_security_gate_output()` used by PostProcessNode before they
             are surfaced. PostProcessNode gates `result`; this second pass
             guards the fields get_output() adds on top of that, which are read
             straight from state and were never in `result`.

        CONTAINMENT. On a violation this method does not merely flip the status
        and return. The base envelope's `output` key is built by the framework
        from `formatted_output` or `result`, so returning the base envelope with
        an ERROR status would still hand the caller the un-gated answer text
        inside the error envelope - the status changes, the content ships
        anyway. A violation therefore CLEARS every output-bearing key and
        returns a bare error envelope: no released text, no structured fields.
        """
        base: Dict[str, Any] = super().get_output(state)

        if state.get("status") != AgentStatus.SUCCESS.value:
            return base

        classification_topic = state.get("classification_topic")
        remediation_checklist = from_json(state.get("remediation_checklist"), []) or []
        citations = from_json(state.get("citations"), []) or []

        violation = _security_gate_output(
            {
                "classification_topic": classification_topic,
                "remediation_checklist": remediation_checklist,
                "citations": citations,
            }
        )
        if violation:
            return self._contained_error_envelope(base)

        base["classification_topic"] = classification_topic
        base["remediation_checklist"] = remediation_checklist
        base["citations"] = citations
        return base

    @staticmethod
    def _contained_error_envelope(base: Dict[str, Any]) -> Dict[str, Any]:
        """Strip every output-bearing key from an envelope that failed the gate.

        Keeps only the correlation keys a caller needs to trace the request.
        Anything that could carry released text - the rendered answer, the
        structured fields - is dropped rather than emptied in place, so no
        partial or best-effort payload survives.
        """
        contained = {key: base.get(key) for key in ("trace_id", "correlation_id", "node_history") if key in base}
        contained["output"] = None
        contained["status"] = AgentStatus.ERROR.value
        return contained


# Back-compat alias - config/agent.yaml declares class: "FSATradingComplianceAuditAgent",
# and src/api/server.py imports the class directly. Keep both names pointing at the agent.
Graph = FSATradingComplianceAuditAgent
