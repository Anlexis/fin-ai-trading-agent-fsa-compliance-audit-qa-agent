# Test Specification — FIN-C2-196

**Template ID:** FIN-C2-196
**Template Name:** FSATradingComplianceAuditAgent
**Category:** Cat 2 (nested RAG)

This document defines the test cases for the implementation (state, nodes,
inner/outer graphs, configuration, server). The test code lives in
`tests/unit/`, `tests/integration/` and `tests/proof_of_boundary/`; this spec
is the contract those tests implement.

## 1. Scope & Invocation Conventions

- Per-node unit tests for the 5 inner domain nodes + the 2 outer gate nodes.
- Configuration consistency (`config/agent.yaml` + `config/config.yaml` ↔ code)
  and seeded knowledge-base integrity.
- Retrieval quality (golden queries over `config/kb/fsa_mrm_kb.json`).
- The caller-input contract: the prompt-injection screen
  (`test_injection_screen.py`) and the intake branch and its path-callable
  annotation (`test_intake_routing.py`).
- Inner-graph (`DomainWorkflowGraph`) and outer-graph
  (`FSATradingComplianceAuditAgent`) composition / integration.
- End-to-end `/invoke` through the real ASGI app with Bearer auth
  (`tests/integration/test_e2e_invoke.py`).
- Proof-of-Boundary: import isolation, State checkpoint safety, invoke order
  (PB-6), HITL propagation (PB-7, conditional), server boot, and output
  containment on a gate violation (`test_output_containment.py`).

**Trust-gate invocation canon.** Every per-node test invokes the node via
`node(state)` — through `BaseNode.__call__`, which runs the trust gate, then
the input gate, then `execute()`, then the credential gate — never a bare
`node.execute(state)`. The state builder sets `caller_trust_level` to
`TrustLevel.VERIFIED_EXTERNAL.value` for the two outer gate slots
(PreProcessNode / PostProcessNode — the manifest's declared caller level) and
`TrustLevel.ANONYMOUS.value` for the five inner domain nodes.

**One deliberate exception.** The injection-screen tests exercise the screen
function directly, and the intake-rejection tests call the node without a
framework wrapper in front. That is the point: a refusal test that passes only
because a framework gate happened to fire proves nothing about the template,
and would pass just as well where that gate is absent or configured off.

**No config carve-out:** nodes do not accept a `config` parameter —
`execute(self, state)` is the only signature. Every test, including config-knob tests (`RetrieveNode` `kb_path`/
`top_k`, `RerankFilterNode` `top_k`/`score_threshold`), invokes through
`node(state)`; the knob is exercised by seeding the state-level
`retrieval_config` field beforehand.

**Input-masking expectations.** The framework input gate masks
`user_input`/`validated_input`/`llm_response` (e-mail, phone/SSN/CC digit
groups, Title-Case name bigrams) to `[MASKED]` before `execute()` runs.
Positive-path payloads are therefore lowercase-or-mixed, identifier-free regulatory
phrasing verified directly against the installed
`shared.security.pii_detector`; intentional-PII tests assert the raw
identifier is gone and the masked marker (`[MASKED]`, or `[REDACTED]` for the
node's own IBAN screen) is present. Domain fields (`compliance_analysis`,
`formatted_answer`, `retrieved_passages`, `citations`, …) are not input-scan
targets.

**Audit muting.** `shared.*` is never sys.modules-stubbed (the framework
imports `shared.security` at load time). The domain audit emitter is muted via
an autouse fixture (`tests/unit/conftest.py`) patching
`src.nodes.<mod>.emit_trace_event`; audit-assertion tests re-patch the same
attribute with a spy and assert on `call.args[1]` (the event payload), never
the whole-call repr.

## 2. Unit Test Cases

### 2.1 PreProcessNode (outer pre_process slot) — `test_pre_process_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| PRE-01 | Valid query | PII-free regulatory question | `status=SUCCESS`, `validated_input` set, `enriched_context` carries channel/source |
| PRE-02 | Empty input | `""` / whitespace | `status=ERROR`, `error_log` non-empty, no `validated_input` |
| PRE-03 | Missing / non-string input | `user_input` absent; dict payload | `status=ERROR` |
| PRE-04 | Identifier screen | IBAN → node screen; e-mail / 4-4-4 digit groups / Title-Case bigram ("Bank Act") → framework input gate | raw identifier absent from `validated_input`; `[REDACTED]` (IBAN) / `[MASKED]` (framework) present |
| PRE-08 | Audit | valid query | `pre_process_complete` emitted; payload (`call.args[1]`) carries `input_chars`; `pre_process_rejected` emitted on empty input with `reason=empty_input` |
| PRE-09 | Injection screen | control token (`<|im_start|>`, `[INST]`, `<<SYS>>`), directive phrasing, markup-spliced directive, hostile field NAME | `status=ERROR`; no `validated_input`; the finding CLASS is logged, never the payload |
| PRE-10 | Unknown context field | `input_context` carrying an undeclared key | `status=ERROR`; the field name is MASKED in the error, never echoed |
| PRE-11 | Context redaction | identifier in a free-text context field | redacted before the value reaches state |

### 2.2 InputValidateNode (inner node 1) — `test_input_validate_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| VAL-01 | Plain text | free-text query | whole string becomes `search_query`; unclassified → filters `{topic: None, top_k: None}`; keyword match → `classification_topic` set |
| VAL-02 | Whitespace | ragged spacing/newlines | collapsed to single spaces |
| VAL-03 | JSON envelope | `{"query","topic","top_k"}` | all three parsed; `question` alias accepted; invalid `topic` override dropped with a note, falls back to automatic classification |
| VAL-04 | Malformed JSON | `{`-prefixed non-JSON | treated as plain-text query + parse note |
| VAL-05 | top_k out of range | 99 / −5 | clamped to 20 / 1 + note |
| VAL-06 | top_k non-numeric | `"many"` | dropped (None) + note |
| VAL-08 | Oversize query | > 2000 chars | truncated to 2000 + note |
| VAL-09 | Empty request | `""` | `search_query=""`, `classification_topic=None` + "empty request" note (non-fatal) |
| — | Checkpoint safety | any | `query_filters` is a JSON string, never a bare dict |
| VAL-05 | Caller numbers | `top_k` / `score_threshold`: out of range, non-numeric, `NaN`, `±Infinity` (string AND raw float), `bool`, non-whole | `status=ERROR`, `intake_rejected=True`, no `query_filters`; the error names the FIELD, never the value |
| VAL-07 | Caller strings | `case_ref` outside `[a-z0-9_]{1,32}` | refused; an inert value is accepted and rendered |
| — | Channel precedence | same field on both channels | the structured `input_context` value wins |

### 2.3 RetrieveNode (inner node 2) — `test_retrieve_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| RET-01 | Happy path | definitive-judgment query | top-1 candidate is `fsa-mrm-002` |
| RET-02 | Ordering | definitive-judgment query | scores strictly sorted desc; all > 0 |
| RET-03 | Entry shape | any hit | keys `{id,title,category,source,score,excerpt}`; excerpt ≤ 400 chars |
| RET-04 | Topic filter | `query_filters.topic="mrm_documentation"` | only `mrm_documentation` entries; top-1 `fsa-mrm-005` |
| RET-05 | Empty / out-of-domain query | `""` / off-topic text | no candidates |
| RET-06 | State-seeded config, unreadable kb_path | bogus `retrieval_config.kb_path` | `[]` + "not readable" note |
| RET-07 | State-seeded config, valid kb_path | explicit `retrieval_config.kb_path` | resolves normally, same top-1 as RET-01 |
| RET-08 | Notes accumulation | prior `intake_notes` | appended, never clobbered |

### 2.4 RerankFilterNode (inner node 3) — `test_rerank_filter_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| RRF-01 | Relevance floor | scores 0.9 / 0.1 | 0.1 dropped (default 0.25 floor) |
| RRF-02 | State-seeded `score_threshold` | `retrieval_config.score_threshold=0.5` | 0.3 dropped |
| RRF-03 | State-seeded `top_k` | `retrieval_config.top_k=1` | one survivor, highest score |
| RRF-04 | Topic boost | matching topic | +0.1, re-ranked ahead |
| RRF-05 | Boost cap | 0.95 + boost | capped at 1.0 |
| RRF-06 | Caller top_k | stricter (1) wins; looser (10) does not widen | enforced |
| RRF-07 | Garbage entries | non-dict / uncoercible score | skipped / coerced to 0.0 and dropped |
| RRF-08 | Tie-break | equal scores | deterministic id-ascending order |

### 2.5 GenerateAnswerNode (inner node 4) — `test_generate_answer_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| GEN-01 | Citation markers | 2 ranked passages | `[1]`/`[2]` markers with titles |
| GEN-02 | Lead sentence | query present | query quoted in the "inform this question" line |
| GEN-03 | Citations list | ranked passages | refs 1..n mirror ranked order; id/title/source carried |
| GEN-04 | Groundedness | single passage | answer body traces to ranked passages only |
| GEN-05 | No coverage | empty/missing `ranked_passages` | escalation analysis; `citations=[]` |
| — | Topic label line | classified vs unclassified | hedged label ("appears most related to…") vs the unclassified fallback line |
| — | Remediation checklist | 4 fixed topics + unclassified | topic-specific 4-item checklist, or the 3-item default fallback |

### 2.6 OutputFormatNode (inner node 5, terminal) — `test_output_format_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| FMT-01 | Full compose | body + citations + checklist | header + body + `## Sources` rows + `## Documentation / Remediation Checklist` rows + legal disclaimer; `status=SUCCESS` |
| FMT-02 | Blank source | citation without source | no `()` suffix |
| FMT-03 | Disclaimer | every input | disclaimer rides with every answer, including a blocked one |
| FMT-04 | No citations / no checklist | empty lists | explicit "- none (…)" / "- none" lines |
| FMT-05 | Missing body | no `compliance_analysis` | fallback text; `status=SUCCESS` |
| — | Verdict control (analysis body) | assertive verdict phrasing (`without a doubt`, `と断定`, `this guarantees …`, …) | entire analysis replaced with the safe hedge-and-escalate fallback; `status` stays `SUCCESS` (a content substitution, not an error); audit payload records `definitive_verdict_blocked_in_analysis=True` |
| — | Verdict control (composed answer) | verdict phrasing reaching the answer via a source title or a checklist item | `status=ERROR`; the whole answer withheld; no fragment of the original released |
| — | Verdict control (caller's words) | a question quoting prohibited phrasing | ANSWERED, not withheld; the question is rendered as attributed quoted material |
| — | Disclaimer | every path, including empty analysis | disclaimer present; a composition that drops it withholds the answer |
| — | Hedged phrasing | non-assertive analysis text | passes through unblocked |

### 2.7 PostProcessNode (outer post_process slot) — `test_post_process_node.py`

| ID | Case | Input (`result`) | Expected |
|----|------|------------------|----------|
| POST-01 | Clean output | compliance answer | `formatted_output=result`, `status=SUCCESS` |
| POST-02 | Empty / missing result | `""` / key absent | forwarded as-is, `status=SUCCESS` (non-fatal) |
| POST-03..06 | Credential leak | `sk-` API key / `password=` assignment / JWT (built at runtime) / Bearer token | `formatted_output` + `result` replaced with the sanitised stub, `status=ERROR`, raw secret absent from both |
| — | Recursive scan | credential nested inside a dict / list | `_security_gate_output()` finds it at any depth |

### 2.8 Manifest / config consistency — `test_config_manifest.py`

| ID | Case | Expected |
|----|------|----------|
| CFG-01 | Template ids | `agent_id` = `template_id` = `agent.id` = `FIN-C2-196` |
| CFG-02 | Class-name contract | manifest `agent.class`/`agent.name` == `FSATradingComplianceAuditAgent` (graph.py class); `module=src.graph` |
| CFG-03 | Classification | Cat 2 / FIN / RAGAgent |
| CFG-04 | Trust level | manifest `VERIFIED_EXTERNAL` == PreProcessNode & PostProcessNode `required_trust_level` |
| CFG-05 | max_retry | int, `0 ≤ v < 10` (framework ceiling); hitl not enabled (PB-7 waiver contract) |
| CFG-06 | Retrieval block | `top_k`/`score_threshold` mirror node module defaults; `kb_path` exists |
| CFG-07 | `_parent_config()` | forwards manifest retrieval + llm blocks; never `{}` |
| — | KB integrity | JSON list ≥ 5 entries; unique ids; required keys per entry; all 4 fixed topics represented |

### 2.9 Retrieval quality (golden queries) — `test_retrieval_quality.py`

| ID | Case | Expected |
|----|------|----------|
| QUAL-01 | 8 golden domain queries (one per non-trivial KB entry cluster) | expected KB entry is top-1 (captured from a real run of InputValidateNode → RetrieveNode → RerankFilterNode, not hand-computed) |
| QUAL-02 | Relevance floor | every survivor ≥ 0.25 |
| QUAL-03 | Citation integrity | every survivor id exists in the seeded KB |
| QUAL-04 | Precision | definitive-judgment query keeps only the 3 `definitive_judgment`-category entries |
| QUAL-05 | Topic override | explicit `topic` override restricts to that category; top-1 `fsa-mrm-005` |
| QUAL-06 | No coverage | out-of-domain query → zero survivors |
| QUAL-07 | Escalation analysis | no-coverage → explicit escalation text, no citations |

## 3. Integration / Composition

### 3.1 Inner graph — `test_domain_workflow_graph.py`

| ID | Case | Expected |
|----|------|----------|
| INT-01 | Composition | inherits `BaseGraph`; registers exactly the 5 domain nodes; no initialize/finalize |
| INT-02 | Config forwarding | `_extra_initial_state()` republishes the retrieval block as the JSON-string `retrieval_config` |
| INT-03 | Output shape | `get_output()` emits `formatted_answer`/`citations`/`classification_topic`/`remediation_checklist`/`status`/… (the merge contract); `route()` → END on error |
| INT-04 | Inner e2e | full inner `invoke()` → SUCCESS; formatted answer + disclaimer + `fsa-mrm-002` top citation; inner `node_history` = the 5 domain nodes in linear order |

### 3.2 Outer graph + e2e — `test_graph_composition.py`

| ID | Case | Expected |
|----|------|----------|
| INT-05 | Outer composition | inherits `AgentBaseGraph` directly; `Graph` alias; `add_edges()` NOT overridden |
| INT-06 | Backbone slots | compile() fills all 5; pre/main/post are PreProcessNode / ComplianceAuditGraphNode / PostProcessNode |
| INT-07 | `get_subgraph()` | returns `DomainWorkflowGraph` carrying the forwarded retrieval config |
| INT-08 | `extract_input()` | prefers `validated_input`, falls back to `user_input` |
| INT-09 | `merge_output()` | inner `formatted_answer` → outer `compliance_answer` AND `result`; `citations`/`classification_topic`/`remediation_checklist`/`status` mapped; changed keys only |
| INT-10 | Manifest fallback | `_parent_config()` never `{}` even with an unreadable manifest |
| INT-11 | e2e happy path | VERIFIED_EXTERNAL invoke → SUCCESS; `output` = gated formatted answer; PostProcessNode traversed |
| INT-12 | e2e trust denial | ANONYMOUS invoke → ERROR; empty `output`; PostProcessNode NOT traversed |
| — | `get_output()` structured fields | non-SUCCESS → base envelope only; SUCCESS → decoded `classification_topic`/`remediation_checklist`/`citations`; a credential-shaped structured field returns a CONTAINED error envelope — status ERROR, `output` cleared, no structured fields, no traceback, no source paths (second, independent scan) |
| — | JSON-string helpers | `to_json`/`from_json` round-trip; None/malformed handling |

## 4. Proof-of-Boundary

| ID | Case | Expected |
|----|------|----------|
| PB-IMPORT | `test_import_isolation.py` | no `agenticstar` / Level-0 import anywhere under `src/` |
| PB-STATE | `test_state_safety.py` | `State` has no credential-named fields and no `BaseModel` / `InvocationContext` annotations |
| PB-6 | `test_pb_invoke_order.py` | full `Graph().invoke()` with `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` (never `for_internal()`) over the payload read from `deploy/invoke_payload.json` (byte-equal by construction) → SUCCESS with outer `node_history` exactly `[InitializeNode, PreProcessNode, ComplianceAuditGraphNode, PostProcessNode, FinalizeNode]` |
| PB-7 | `test_pb7_hitl_interrupt_propagation.py` | **Auto-waived — non-HITL** (`config/agent.yaml` has no `hitl.enabled: true`; `ComplianceAuditGraphNode.propagate_hitl = False`); canonical conditional skip-stub (copied verbatim from a peer template) retained |
| PB-BOOT | `test_server_boot.py` | `import src.api.server` does not raise; module-level agent is this template's class, compiled; fresh ctor→`compile()` fills the 5 backbone slots; `/health` reports the agent |

> **Boundary gate checklist:** PB-IMPORT, PB-STATE, PB-6 and PB-BOOT are
> mandatory. PB-7 applies only to HITL-enabled templates — this template is
> non-HITL, so PB-7 is **Auto-waived — non-HITL** and its skip must not block
> the gate.

## 5. Test Execution Summary

- Execution date: 2026-07-29
- Runner: real SDK (`agenticstar-agentcore==1.0.0`), `python -m pytest tests/`
- Total tests: 163
- Pass: 162 / Fail: 0 / Skip: 1 (PB-7 conditional stub — auto-waived, non-HITL)
- Determinism: no model call, no network; retrieval + answer assembly are rule-based
  over the seeded FSA-MRM knowledge base
