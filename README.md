# FIN-C2-196 — FSA Trading Compliance Audit Agent

> **Category**: Cat 2 (a domain pipeline: a fixed multi-step workflow for one job-to-be-done)
> **Industry**: FIN (financial services)

## Overview

This agent answers a compliance or legal team's question about what a Japanese Financial
Services Agency (FSA) rule requires of an AI trading system's output. It classifies the question
into one of four regulatory topics — definitive-judgment prohibition (Financial Instruments and
Exchange Act Article 38-2), AI model-risk-management documentation, the 48-hour incident
notification duty, and mandatory human-oversight gates — retrieves the supporting passages from a
curated knowledge base of the regulatory text and guidance, and returns a grounded analysis with
numbered citations plus a documentation and remediation checklist.

Two content rules are built into the pipeline and cannot be switched off by a caller, a flag, or a
state field. Every answer carries a legal disclaimer stating that it is informational and not legal
advice; and the analysis is scanned for unconditional verdict phrasing, with the whole body replaced
by an escalate-to-a-human message if any is found — an agent that audits others for making
definitive claims must not make them itself.

Scope is classification and analysis only. The agent files nothing, notifies no one, and gives no
investment advice.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform environment. It is not declared in `pyproject.toml` because it does not resolve from the public package index. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded mode.
The HTTP entry point provisions its secrets from the platform's secrets provider at import time and
the agent compiles its graph against the installed framework; without the platform neither is
available, so start-up fails rather than serving from a partially initialised agent. This is
intentional — a half-running compliance agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the architecture and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
