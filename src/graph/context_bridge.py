"""AgentCore Platform v1.0"""

# FIN-C2-196 - caller-context bridge between the outer and inner graph.
#
# Why this exists
# ---------------
# The framework's GraphNode calls `subgraph.invoke(user_input, session_id=...,
# ctx=...)`. It passes no `input_context`, so a nested (two-layer) agent's inner
# graph never receives the structured caller channel that the outer graph was
# invoked with: the inner graph's `input_context` is always an empty dict.
#
# The string channel (`extract_input()` -> the inner graph's `user_input`) is the
# only value that crosses the boundary on its own. Rather than smuggling
# structured parameters through that string, the outer node stashes the caller
# context in a ContextVar immediately before the inner graph is invoked, and the
# inner graph reads it back in `_extra_initial_state()` to seed its own state.
#
# A ContextVar is the right carrier here: `extract_input()` and the inner graph's
# state seeding happen in the same execution context, and ContextVars are
# per-context rather than global, so concurrent invocations do not see each
# other's caller data.
#
# The value crossing this bridge is UNVALIDATED caller input. It is validated by
# the inner graph's intake node (InputValidateNode), never here — the bridge is
# transport, not a trust boundary.

from contextvars import ContextVar
from typing import Any, Dict

_CALLER_CONTEXT: ContextVar[Dict[str, Any]] = ContextVar("fin_c2_196_caller_context", default={})


def stash_caller_context(value: Any) -> None:
    """Record the caller context for the inner graph invocation that follows.

    Non-mapping values (or absent context) reset the bridge to an empty mapping
    so a later invocation can never observe an earlier caller's data.
    """
    _CALLER_CONTEXT.set(dict(value) if isinstance(value, dict) else {})


def take_caller_context() -> Dict[str, Any]:
    """Return the stashed caller context and clear the bridge.

    Read-once: the value is cleared as it is handed over, so a subsequent inner
    graph run that was not preceded by a stash sees an empty mapping rather than
    stale data.
    """
    value = _CALLER_CONTEXT.get()
    _CALLER_CONTEXT.set({})
    return dict(value) if isinstance(value, dict) else {}
