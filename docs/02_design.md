# Template Design Specification — FIN-C2-196

**Template ID:** FIN-C2-196
**Template Name:** FSATradingComplianceAuditAgent
**Category:** Cat 2 (multi-step domain workflow — RAG pattern)
**Industry:** FIN

## Position in AgentCore Architecture

- **Agent Class:** `FSATradingComplianceAuditAgent` (alias `Graph`)

| Layer | Base |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` (outer) + `BaseGraph` (inner) — direct framework inheritance |

- **Inner graph base:** `BaseGraph` — `DomainWorkflowGraph`
- **Pattern:** Cat 2 two-layer nested architecture (outer fixed 5-node backbone +
  `GraphNode` in the `main` slot wrapping an inner `BaseGraph` domain workflow)
- **Three-Layer Separation:**
  - State: flat `TypedDict` composition (no Pydantic — msgpack incompatible);
    structured fields stored as JSON strings via `to_json()` / `from_json()`
  - Node: inheritance via `FunctionNode` (override `execute(self, state) -> dict`
    ONLY — no `config` parameter. Config knobs reach nodes via State seeding,
    see "Runtime config forwarding" below)
  - Graph: composition (`register_nodes()` for node substitution); outer
    `add_edges()` is NOT overridden

## Purpose

FSA trading-compliance audit Q&A agent: a compliance officer or legal team asks
whether an AI trading-system output risks a 断定的判断 (definitive/assertive
judgment) under 金融商品取引法 Article 38-2, or asks about the adjacent MRM
documentation / 48-hour incident-notification / human-oversight obligations.
The agent classifies the question into one of four fixed FSA regulatory
topics, grounds the analysis against a seeded FSA-MRM legal knowledge base,
assembles a rule-based grounded compliance analysis with citations, and
attaches a topic-specific documentation/remediation checklist. Every answer
carries a non-suppressible legal disclaimer and passes through a
block-definitive-classification control so the agent itself never issues an
assertive verdict. The pipeline is fully deterministic: keyword retrieval plus
rule-based analysis assembly, with no model call at any step.
Scope is classification/audit only: the agent never files anything with the
FSA, dispatches incident notifications, gives investment advice, or takes any
autonomous action.

## Architecture Overview

### Outer backbone (AgentBaseGraph)

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry, max 3)
                                   pre_process
```

| Slot | Class | Responsibility | required_trust_level | S-gate |
|------|-------|----------------|----------------------|--------|
| initialize | InitializeNode (framework default) | session_id, trust_level, schema_version | — (framework) | — |
| pre_process | `PreProcessNode` | Validate non-empty input; screen both caller channels for prompt injection (fail closed); surface-strip direct identifiers (account numbers, IBAN, e-mail) from the question AND the structured context → `validated_input` | `TrustLevel.VERIFIED_EXTERNAL` | trust gate, input screen, redaction |
| main | `ComplianceAuditGraphNode` (`GraphNode`) | delegates to inner `DomainWorkflowGraph`; maps inner `formatted_answer` / structured fields → outer `result` / `classification_topic` / `remediation_checklist` / `citations` | — (GraphNode delegation) | — (delegates) |
| post_process | `PostProcessNode` | Credential output gate — module-level `_security_gate_output()` scan (recursive over nested dict/list) on `result` → ERROR + sanitised stub | `TrustLevel.VERIFIED_EXTERNAL` | output gate |
| finalize | FinalizeNode (framework default) | response_metadata, total_time_ms | — (framework) | — |

### Inner graph (DomainWorkflowGraph — BaseGraph, linear)

```
START → input_validate → retrieve → rerank_filter → generate_answer → output_format → END
```

All five inner domain nodes declare `required_trust_level = TrustLevel.ANONYMOUS`
(the external trust gate lives on the outer backbone gate node; a stricter inner
level would deny a real VERIFIED_EXTERNAL invoke at runtime).

| Node | Responsibility | required_trust_level | Input State | Output State |
|------|----------------|----------------------|-------------|--------------|
| `InputValidateNode` | Parse the (possibly JSON-enveloped) question; normalise whitespace; cap length; guard numeric params (`top_k` 1–20); classify the question into one of the four fixed FSA topics (deterministic keyword scoring; a caller-supplied `topic` override is honoured when valid) | `TrustLevel.ANONYMOUS` | `validated_input` \| `user_input` | `search_query`, `query_filters`, `classification_topic`, `intake_notes` |
| `RetrieveNode` | Deterministic keyword retrieval over the seeded FSA-MRM legal KB (`config/kb/fsa_mrm_kb.json`): tokenise query, score title/tags/content overlap, apply the topic filter | `TrustLevel.ANONYMOUS` | `search_query`, `query_filters`, `retrieval_config` | `retrieved_passages`, `intake_notes` |
| `RerankFilterNode` | Rerank candidates (topic-match boost), drop entries below `score_threshold`, cap at `top_k` | `TrustLevel.ANONYMOUS` | `retrieved_passages`, `query_filters`, `retrieval_config` | `ranked_passages` |
| `GenerateAnswerNode` | Rule-based grounded compliance analysis assembled from the ranked knowledge-base passages only, with numbered citation markers, plus the topic-specific documentation/remediation checklist. The caller's question is emitted separately as `audited_question`, never inlined into the analysis body | `TrustLevel.ANONYMOUS` | `ranked_passages`, `search_query`, `classification_topic` | `compliance_analysis`, `audited_question`, `citations`, `remediation_checklist` |
| `OutputFormatNode` | Compose the final answer: hedged classification line + analysis body + Sources + Documentation/Remediation Checklist, run the block-definitive-classification scan, then append the standing non-suppressible legal disclaimer (both are part of this node, NOT post_process) | `TrustLevel.ANONYMOUS` | `compliance_analysis`, `citations`, `remediation_checklist`, `classification_topic` | `formatted_answer`, `status` |

### The four fixed FSA regulatory topics

| Topic slug | Subject |
|---|---|
| `definitive_judgment` | 断定的判断 (definitive/assertive judgment) under 金商法 Article 38-2 |
| `mrm_documentation` | AI Model Risk Management documentation requirements |
| `incident_notification` | 48-hour FSA incident-notification duty |
| `human_oversight` | Mandatory human-oversight gates before customer-facing AI output |

A question that matches no topic keywords classifies as `None` (unclassified);
retrieval then runs without a topic filter and the checklist falls back to a
generic "route to compliance for manual classification" list.

### Data Flow

```
user_input
  → PreProcessNode (validate/screen/redact)      → validated_input
  → ComplianceAuditGraphNode.extract_input       → inner DomainWorkflowGraph.invoke(validated_input)
        → input_validate                         → search_query / query_filters / classification_topic
        → retrieve                               → retrieved_passages
        → rerank_filter                          → ranked_passages
        → generate_answer                        → compliance_analysis / citations / remediation_checklist
        → output_format                          → formatted_answer (+ disclaimer, gated by
                                                     block-definitive-classification)
     get_output() → {formatted_answer, citations, classification_topic,
                      remediation_checklist, status, ...}
  → ComplianceAuditGraphNode.merge_output         → result / classification_topic /
                                                      remediation_checklist / citations
  → PostProcessNode (credential gate)             → formatted_output (gated)
```

Structured parameters travel as JSON through `extract_input()`: when the caller
supplies a JSON envelope (`{"query": ..., "topic": ..., "top_k": ...}`), it
passes through `validated_input` as a string and the FIRST inner node
(`InputValidateNode`) parses it back. Inner nodes read their input via
`state.get("validated_input") or state.get("user_input", "")`.

### Runtime config forwarding (`_parent_config`)

Two configuration files, with distinct jobs:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | The static manifest, flat — every key at root level. Identity, entry point, trust level, and the compile-time `requires` declarations | The registry, at discovery |
| `config/config.yaml` | Runtime parameters: `max_retry`, `timeout_s`, and the `retrieval` tuning block | `_parent_config()`, at each run |

`ComplianceAuditGraphNode._parent_config()` loads **`config/config.yaml`** and
forwards the `retrieval` block under `config["configurable"]` (never `{}`):

```
{"configurable": {"retrieval": {top_k, score_threshold, kb_path}}}
```

The distinction matters at runtime, not only on paper: the manifest is flat and
carries no nested runtime block, so a reader pointed at it would find nothing,
fall back to its defaults, and leave every declared value inert while still
appearing configured. The declared values are covered by a test that changes
one and asserts the result changes.

`get_subgraph()` passes the forwarded config into `DomainWorkflowGraph(config=...)`
(graph-level constructor injection of immutable config — distinct from the
per-node `execute()` contract below); the inner graph republishes the
`retrieval` block into the inner initial state as the JSON-string field
`retrieval_config` (via `_extra_initial_state()`), so the declared `top_k` /
`score_threshold` are live at runtime. `RetrieveNode` and `RerankFilterNode`
read them from that state field, falling back to module defaults that mirror
the configured values — neither node's `execute()` takes a `config` parameter.

### Caller-context forwarding (the bridge)

The framework's `GraphNode` invokes a subgraph with the string input only; it
does not pass `input_context` through. A nested agent therefore has to carry
the structured caller channel across the boundary itself.
`ComplianceAuditGraphNode.extract_input()` stashes the caller context in a
ContextVar (`src/graph/context_bridge.py`) immediately before the inner graph
is invoked, and `DomainWorkflowGraph._extra_initial_state()` reads it back and
seeds it as `caller_context`. The value crossing the bridge is unvalidated;
`InputValidateNode` is its only consumer and validates it there.

### State Definition

| Field | Type | Purpose | Layer |
|-------|------|---------|-------|
| `validated_input` | `NotRequired[str]` | identifier-stripped question payload | outer |
| `compliance_answer` | `NotRequired[str]` | final answer, mapped from inner `formatted_answer` | outer |
| `classification_topic` | `NotRequired[Optional[str]]` | resolved FSA topic slug (or `None`) | outer + inner |
| `remediation_checklist` | `NotRequired[Optional[str]]` (JSON) | topic-specific documentation/remediation checklist | outer + inner |
| `search_query` | `NotRequired[str]` | normalised compliance question | inner |
| `query_filters` | `NotRequired[Optional[str]]` (JSON) | parsed structured params (`topic`, `top_k`) | inner |
| `retrieval_config` | `NotRequired[Optional[str]]` (JSON) | forwarded manifest `retrieval` block | inner |
| `retrieved_passages` | `NotRequired[Optional[str]]` (JSON) | scored KB candidates | inner |
| `ranked_passages` | `NotRequired[Optional[str]]` (JSON) | reranked + threshold-filtered passages | inner |
| `compliance_analysis` | `NotRequired[str]` | rule-assembled grounded analysis body | inner |
| `citations` | `NotRequired[Optional[str]]` (JSON) | `[{ref, id, title, source}]` | outer + inner |
| `formatted_answer` | `NotRequired[str]` | final answer + sources + checklist + disclaimer | inner |
| `intake_notes` | `NotRequired[Optional[str]]` (JSON) | validation / parse notes (no PII) | inner |
| `trace_id` / `correlation_id` | `Optional[str]` | framework-managed tracing | both |

**State Constraints (mandatory):**
- Flat `TypedDict` only (primitives + JSON-serialisable types).
- Structured fields (dict / list[dict]) stored as JSON STRINGS via `to_json()` /
  `from_json()` — used consistently by every producer AND consumer (checkpoint
  msgpack safety).
- Domain fields are `NotRequired[...]` (valid TypedDict before any node writes).
- `formatted_output` is NOT re-declared (backbone field stays framework-owned).
- No JWT, API keys, credentials, or raw personal identifiers in State.
- `InvocationContext` via `config["configurable"]` only (never in State).
- No Pydantic models / dataclasses / arbitrary Python objects.

## Security Gates

- **Trust gate:** every node declares `required_trust_level` (see tables
  above); `PreProcessNode` (VERIFIED_EXTERNAL) rejects empty / non-string
  `user_input` before the inner workflow runs. The standalone server elevates
  authenticated Bearer callers to VERIFIED_EXTERNAL (`INVOKE_AUTH_TOKEN`).
- **Caller-input contract (fail closed):** every caller-supplied number
  (`top_k`, `score_threshold`) passes a finite-and-bounded parser in
  `InputValidateNode`, and every caller string that reaches the rendered report
  (`case_ref`) is locked to `[a-z0-9_]{1,32}`. A field outside its bounds ends
  the run with an error naming the FIELD; the value is never echoed. Finiteness
  is checked explicitly rather than left to the range test: `NaN` parses
  through `float()`, arrives intact through a JSON body, and compares False
  against everything — so a range check built from comparisons cannot see it,
  and a `NaN` relevance floor would silently discard every passage while the
  run reported success. Unrecognised context fields are refused, and their
  names are masked rather than echoed.
- **Prompt-injection screen (template-owned):** `PreProcessNode` screens both
  caller channels itself rather than relying on the framework alone — a
  template that delegates entirely is fail-open wherever that gate is absent or
  configured off. Chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`)
  are matched as a CLASS, not as a list of known payloads, because the
  delimiter itself is the attempt to forge a turn boundary. The scan walks
  parsed structures depth-first INCLUDING KEYS, so a payload hidden in a field
  name or behind a `\u` escape is still seen, and it runs both BEFORE and AFTER
  redaction: a strip can remove a control token and forward the surrounding
  directive as ordinary prose, and it can also re-assemble a directive that was
  spliced with markup. Probed in both directions — the repo's own knowledge
  base and checklists must never trip it.
- **Identifier screen:** `PreProcessNode._surface_strip_identifiers()` redacts
  IBAN, long account-number, and e-mail patterns from the question AND from the
  free-text fields of the structured caller context; the framework's default
  input scan additionally masks `user_input` / `validated_input` at every node
  boundary.
- **Credential output gate:** `PostProcessNode` calls the module-level
  `_security_gate_output()` scan from `execute()` — API keys / JWT / Bearer
  tokens / credential assignments in the final `result` replace the output with
  a sanitised stub and return `AgentStatus.ERROR`. The scan walks nested
  dict/list structures recursively (not top-level-string-only), and the same
  function is reused by `FSATradingComplianceAuditAgent.get_output()` before it
  surfaces any structured field — vetted scalar fields only
  (`classification_topic`, `remediation_checklist` items, `citations` id/title/
  source/ref), never a raw request/response dict, fail-closed on any violation.
  No `_extra_security_gate_input` / `_extra_security_gate_output` instance
  methods are defined on any node — the framework auto-wraps such hooks.
- **Containment on a violation:** a gate that detects a violation CLEARS the
  output-bearing fields; it does not merely relabel the response. The framework
  builds the caller-facing envelope from `formatted_output` or `result`
  regardless of status, so returning an error envelope without clearing would
  ship the un-gated answer with an error label attached.
  `get_output()` therefore returns a bare envelope carrying only the
  correlation keys — no answer text, no structured fields, no traceback, no
  source paths.
- **Audit logging:** every node's `execute()` emits exactly ONE
  domain-specific `emit_trace_event("<node>_complete", {small non-PII payload},
  state)` (free function, positional args) on its success path (some nodes
  additionally emit on a rejection path — never PII). Nodes do NOT emit
  `node_start` / `node_complete` / `node_error` — `BaseNode.__call__()` emits
  those. Domain event names:
  - `pre_process_complete` (+ `pre_process_rejected` on empty input, an
    unrecognised context field, or an injection finding)
  - `input_validate_complete` (+ `input_validate_rejected` on a caller field
    that failed validation — the field name and reason, never the value)
  - `retrieve_complete`
  - `rerank_filter_complete`
  - `generate_answer_complete`
  - `output_format_complete` (+ `output_format_withheld` when the composed
    answer breached the output invariant)
  - `post_process_complete` (+ `post_process_blocked` on a credential violation)

## The output invariant

This template states two rules about its own output, and `OutputFormatNode`
enforces both against the COMPOSED answer rather than against the fragment
that produced it:

1. **Every answer carries the legal disclaimer** ("not legal advice, consult
   qualified counsel", plus the informational-only framing). It is appended
   unconditionally, and its presence is then VERIFIED rather than assumed —
   the renderer and the check are separate functions, so a composition that
   dropped the line is detected instead of shipped.
2. **This agent never itself states an unconditional verdict.** The scan runs
   over the classification line, the analysis body, the checklist and the
   rendered source titles — every representation the agent authored. Checking
   only the analysis body would leave the other four unchecked, and an
   invariant that holds for the convenient representation is not an invariant.
   On a breach the whole answer is withheld: no fragment of the original text
   is released.

**What the verdict rule does NOT police: the caller's own question.** It is
rendered as attributed quoted material and excluded from the scan. That
distinction is load-bearing rather than cosmetic. A compliance officer asking
whether wording that "guarantees" a return is a prohibited definitive judgment
is this template's central use case, and the same word is one of the keywords
the classifier uses to route that question — so a rule that scanned the
caller's words would suppress the answer to precisely the question the
template exists to answer. Each pattern additionally requires a verdict
CONSTRUCTION (an assertion verb bound to its object) rather than a bare
keyword, so ordinary domain vocabulary does not trip it.

**Numeric precision.** This template renders no monetary aggregates: its
output is a classification, a set of cited passages and a checklist, and the
only numbers in it are citation markers. The rounding grid that money-bearing
templates enforce at their output boundary is therefore not applicable here,
and the two rules above are the invariant this boundary enforces instead.

## HITL

`hitl.enabled` stays `false`: no step in the pipeline requires a live human
pause. The compliance boundary is enforced automatically by the output rules
above, and human review happens downstream, when the compliance officer reads
the advisory analysis. The agent takes no externally-impactful action — it
files nothing, dispatches no notification, gives no investment advice and acts
autonomously nowhere — so no pre-action approval gate applies.

## Implementation note — deterministic assembly

This template is **deterministic end-to-end**: retrieval is keyword scoring
over the seeded knowledge base, and `GenerateAnswerNode` assembles the grounded
compliance analysis from the ranked passages by rule (hedged classification
line + cited passage excerpts + topic checklist). There is no model call and
no model-client dependency anywhere in the pipeline; the manifest declares
`generation_mode: deterministic` and an empty `requires.extras` to match, and
no `system_prompt` is read at runtime.

The prompt at `config/prompts/compliance_analysis_prompt.md` documents the
analysis contract for an implementation that generates the body with a model
instead: same `ranked_passages` input, same `compliance_analysis` /
`citations` / `remediation_checklist` state keys, so no other node changes.
Such an implementation would add its own model configuration; none is shipped
here, because a configuration block nothing reads is a promise the code does
not keep.

## Composition Pattern

- **Pattern:** `GraphNode` (subgraph) in the outer `main` slot.
- **Composition target:** `DomainWorkflowGraph` (inner `BaseGraph`).
- **Error propagation strategy:** `propagate` (inner errors re-raised as `SubgraphError`).
- Inner domain nodes run at `TrustLevel.ANONYMOUS`; outer pre/post_process run
  at `TrustLevel.VERIFIED_EXTERNAL`.

## Import Isolation Confirmation
- [x] Template does not import the platform SDK.
- [x] Import targets: `framework/` and `shared/` only.
- [x] Base positions name framework base classes only.

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base class | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step audit workflow, no autonomous loop |
| Composition pattern | Standalone Cat 1 slots | GraphNode → inner BaseGraph | **GraphNode → inner BaseGraph** | 5-step domain workflow exceeds a single `main` node; nested keeps the outer backbone untouched |
| Topic classification | Caller-supplied only | Deterministic keyword classification + optional caller override | **Deterministic classification** | The agent must classify the four regulatory topics itself, not merely accept a caller label |
| Answer synthesis | Rule-based assembly | Model call | **Rule-based** | Deterministic assembly is transparent and testable; the documented seam keeps a model-based body a drop-in replacement |
| Knowledge-base storage | External vector store | Seeded JSON corpus | **Seeded JSON corpus** | Self-contained, deterministic, network-free; the retrieval contract (`retrieved_passages` JSON) is store-agnostic for a later vector-store upgrade |
| Definitive-verdict control | Rely on hedged wording only | Rely on hedged wording + an explicit scan/block gate | **Both (defense-in-depth)** | The control is a security requirement, not a style convention: hedged templates can be edited, a gate cannot be talked around |
| Verdict scan scope | The whole composed answer | Agent-authored text only, caller question excluded | **Agent-authored text only** | Scanning the caller's words suppressed the answer to the template's most common question; the rule polices what the agent asserts |
| Invalid caller number | Clamp into range | Refuse the request | **Refuse** | A clamped value answers a question the caller did not ask, and clamping cannot catch NaN at all — every comparison it relies on is false |
