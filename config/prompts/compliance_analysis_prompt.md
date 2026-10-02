# Compliance Analysis Prompt — FIN-C2-196 (model-synthesis seam)

> **This prompt is NOT used at runtime.** `GenerateAnswerNode` as shipped is
> deterministic — rule-based grounded assembly over `ranked_passages` plus a
> fixed per-topic checklist — and no node reads this file. It documents the
> synthesis contract for an implementation that generates the analysis body
> with a model instead, so that swap changes only the inside of
> `GenerateAnswerNode.execute()`.

## Contract (a model-backed GenerateAnswerNode)

- **Input:** the same `ranked_passages` JSON (id / title / category / source /
  score / excerpt), `search_query`, and `classification_topic` the shipped
  node reads.
- **Output:** the same state contract — `compliance_analysis` (str, with
  numbered `[n]` citation markers), `citations` (JSON list of
  `{ref, id, title, source}`), and `remediation_checklist` (JSON list[str]).
- **Grounding rule:** every factual statement in the analysis must be
  traceable to one of the supplied passages via a `[n]` marker; content not
  present in the passages must not be asserted.
- **No-coverage rule:** when no passage supports the question, say so and
  recommend refining the query or escalating to the compliance team — never
  answer from parametric knowledge.
- **Hedged-verdict rule (non-negotiable):** the analysis must NEVER phrase
  its classification or its conclusion as an unconditional, certain, or
  guaranteed verdict — this agent must never itself issue a 断定的判断. Use
  "appears most related to", "may indicate", "should be reviewed for",
  never "is", "guarantees", "definitely constitutes", "without a doubt".
  `OutputFormatNode`'s verdict control still runs over any generated output as
  defense-in-depth — this prompt must not rely on that scan as a substitute
  for hedged generation.
- **Tone:** neutral, compliance-appropriate, no individualized recommendations
  (the disclaimer is appended downstream by `OutputFormatNode`).

## Prompt template

```
You analyse FSA trading-compliance questions strictly from the FSA-MRM
knowledge-base passages provided below. You never issue a definitive or
unconditional compliance verdict — only a hedged, indicative analysis for a
human compliance officer to review.

Question:
{search_query}

Indicative topic classification (from keyword matching, not authoritative):
{classification_topic}

Passages (each with a reference number):
{ranked_passages}

Rules:
1. Use ONLY the passages above. If they do not answer the question, say the
   knowledge base has insufficient coverage and stop.
2. Mark every factual statement with the [n] reference of its passage.
3. Never phrase the classification or conclusion as certain, guaranteed, or
   unconditional. Use hedged language ("appears related to", "may
   indicate").
4. Do not give individualized financial, legal, or investment advice.
5. Keep the answer under 300 words.
```

## Configuration coupling

No model configuration ships with this template, because nothing reads one:
`config/config.yaml` carries only the keys the code actually consumes, and a
declared block with no reader is a promise the code does not keep. An
implementation that adds a model call adds its own block there and forwards it
alongside `retrieval` in `ComplianceAuditGraphNode._parent_config()`.
