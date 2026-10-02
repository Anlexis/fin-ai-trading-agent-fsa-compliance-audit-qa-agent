"""AgentCore Platform v1.0"""

# FIN-C2-196 - RerankFilterNode
# Domain node 3: rerank the retrieval candidates and enforce the relevance
# floor. Deterministic: a small topic-match boost on top of the retrieval
# score, drop everything below `score_threshold`, cap the survivors at
# `top_k`.
#
# Config: reads `top_k` / `score_threshold` from the state-seeded
# `retrieval_config` field, falling back to module defaults that mirror
# config/config.yaml. Caller overrides (query_filters) win when they are
# STRICTER than the configured value - a caller may narrow the result set but
# never widen it past the deployment's own limits. Those overrides were
# validated as finite and in range by InputValidateNode; this node re-checks
# their type at the point of use rather than trusting the state field.
# execute() takes no `config` parameter.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import math
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Defaults mirror the `retrieval` block in config/config.yaml.
_DEFAULT_RETRIEVAL: Dict[str, Any] = {
    "top_k": 4,
    "score_threshold": 0.25,
}

# Boost applied when a candidate's category matches the classified topic.
_TOPIC_BOOST = 0.1


def _finite_or_default(value: Any, default: float) -> float:
    """Parse a float, falling back to `default` unless the result is finite.

    Non-finite values are refused here as well as at intake. NaN survives
    float() and every comparison against it is false, so a NaN relevance floor
    would silently discard every passage while the run reported success; and
    min()/max() clamping does not catch it, because the comparisons that
    clamping relies on are themselves false.
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _resolve_retrieval_config(state: AgentState) -> Dict[str, Any]:
    """Effective retrieval config: state-seeded retrieval_config > defaults."""
    effective = dict(_DEFAULT_RETRIEVAL)  # local copy - never mutate the module default
    from_state = from_json(state.get("retrieval_config"), None)
    if isinstance(from_state, dict):
        effective.update(from_state)
    return effective


class RerankFilterNode(FunctionNode):
    """Rerank candidates, apply the score threshold, cap at top_k.

    Input state keys:
        retrieved_passages: JSON list of scored candidates (from RetrieveNode)
        query_filters:       JSON dict with the classified topic / top_k override
        retrieval_config:    forwarded manifest retrieval block (JSON)

    Output state keys (partial dict):
        ranked_passages: JSON list of surviving passages (score desc, <= top_k)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = from_json(state.get("retrieved_passages"), []) or []
        filters = from_json(state.get("query_filters"), {}) or {}
        retrieval_cfg = _resolve_retrieval_config(state)

        try:
            top_k = int(retrieval_cfg.get("top_k", _DEFAULT_RETRIEVAL["top_k"]))
        except (TypeError, ValueError, OverflowError):
            top_k = int(_DEFAULT_RETRIEVAL["top_k"])
        top_k = max(1, min(20, top_k))
        # A stricter caller override wins. `bool` is excluded explicitly: it is
        # a subclass of int in Python, so True would otherwise read as top_k=1.
        caller_top_k = filters.get("top_k")
        if isinstance(caller_top_k, int) and not isinstance(caller_top_k, bool) and 1 <= caller_top_k < top_k:
            top_k = caller_top_k

        score_threshold = _finite_or_default(
            retrieval_cfg.get("score_threshold"),
            float(_DEFAULT_RETRIEVAL["score_threshold"]),
        )
        score_threshold = max(0.0, min(1.0, score_threshold))
        # A stricter caller relevance floor wins, on the same rule as top_k.
        caller_threshold = filters.get("score_threshold")
        if caller_threshold is not None:
            caller_threshold = _finite_or_default(caller_threshold, score_threshold)
            if score_threshold < caller_threshold <= 1.0:
                score_threshold = caller_threshold

        topic = filters.get("topic")

        reranked: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            entry = dict(candidate)  # local copy - inputs stay immutable
            try:
                score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if topic and str(entry.get("category", "")).lower() == str(topic).lower():
                score = min(1.0, score + _TOPIC_BOOST)
            entry["score"] = round(score, 4)
            reranked.append(entry)

        # Deterministic ordering: score desc, then id asc for stable ties.
        reranked.sort(key=lambda c: (-c.get("score", 0.0), str(c.get("id", ""))))

        kept = [c for c in reranked if c.get("score", 0.0) >= score_threshold][:top_k]
        dropped = len(reranked) - len(kept)

        # Domain audit: rerank + relevance floor applied.
        emit_trace_event(
            "rerank_filter_complete",
            {
                "kept": len(kept),
                "dropped": dropped,
                "score_threshold": score_threshold,
                "top_k": top_k,
            },
            state,
        )

        return {"ranked_passages": to_json(kept)}
