"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, Dict, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FSATradingComplianceAuditAgent

app = FastAPI(title="Agent")

agent = FSATradingComplianceAuditAgent()
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="fin-c2-196", agent_name="FSATradingComplianceAuditAgent"))


# Cap on the structured caller channel, enforced at the adapter so an
# oversized body is refused before it reaches the agent.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller channel. Optional: with no context supplied the agent
    # answers the free-text question using the deployment's configured
    # defaults. Fields are validated by the agent's intake node, not here -
    # this adapter enforces size only.
    input_context: Dict[str, Any] = {}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and are then treated as
    # VERIFIED_EXTERNAL. Trust established by middleware is never demoted.
    # This adapter is the entry-point auth boundary — the standalone equivalent
    # of the platform's auth middleware. The token is a deployment-level caller
    # credential rather than an agent secret, so it is read from the
    # environment: no invocation context exists yet at this point, because
    # establishing who the caller is is what this block does.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    # Size is the adapter's concern; field semantics are the agent's. Measured
    # on the serialised form so the limit reflects what was actually sent.
    if len(json.dumps(req.input_context).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context is too large.")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast(Dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=req.input_context))


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "FSATradingComplianceAuditAgent"}
