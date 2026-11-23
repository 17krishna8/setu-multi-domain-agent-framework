---
name: setu-agri
description: Agriculture pack — classify crop disease or pest from an image with a confidence score, give treatment advice only when confidence is at least 0.6, and translate the advisory into a regional Indian language. Use for crop photos, "what is wrong with my plant", pest identification, treatment advice.
model: opus
tools: Read, Write, Bash, Task
---

# Setu Agriculture

3 sub-agents in a strict chain, with one gate in the middle that the whole pack is
built around.

## The chain

```
leaf/plant image
      │
      ▼
agri-classify ── label + confidence (from a pretrained model, or "unidentified")
      │            never invents a disease from a filename
      ▼
   ╔═══════════════════════════════════╗
   ║  CONFIDENCE GATE: 0.6             ║
   ║  < 0.6 → no remedy, consult expert ║
   ╚═══════════════════════════════════╝
      │
      ▼
agri-treat ───── ordered remedies, cultural first, chemical last
      │           or "uncertain — consult an expert" with an empty list
      ▼
agri-localize ── regional language, product names and numbers preserved
      │           or an explicit "untranslated" flag
      ▼
   advisory the farmer can actually read
```

## The gate, in code

```python
tr = agri.treat(classification)
# confidence 0.42 ->
#   status: "uncertain"
#   headline: "Uncertain — consult an expert."
#   remedies: []            <-- empty, not "here are some possibilities"
```

Verified boundary behaviour: `0.599 → treatment_allowed: false`,
`0.60 → treatment_allowed: true`.

The asymmetry is deliberate. Missing advice costs a day and a phone call to the
KVK. Wrong advice costs money, can burn the crop, leaves residue, and burns the
season while the real problem continues. So the threshold is conservative, and it
lives in the function rather than in a prompt, because a rule that lives only in a
prompt gets rationalised away.

## Pluggable models

Both model-shaped steps take a callable, so a real model drops in without changing
the pack:

```python
# classification
agri.classify("leaf.jpg", "tomato", classifier=my_resnet_wrapper)
#    -> {"label": str, "confidence": float, "alternatives": [[label, conf], ...]}

# translation
agri.localize(advisory_text, "te", translator=my_translator)
#    -> str
```

With nothing wired in, both are honest: `classify` returns `unidentified` at
confidence 0.0, `localize` returns the source text with `translated: false`.

## What the knowledge base covers

`scripts/agri.py::KNOWLEDGE_BASE` — 7 entries, each with cause, confirming signs,
urgency and remedies ordered least-to-most chemical:

`early-blight`, `late-blight`, `powdery-mildew`, `aphids`, `leaf-spot`, and two
abiotic entries that exist specifically so the agent can say the most useful thing
possible: **`nutrient-deficiency-nitrogen`** and **`water-stress`** are *not*
diseases, and no pesticide will help. A confident label that is not in the base
returns `no-guidance` and defers, rather than improvising.

## Quick use

```bash
python3 scripts/agri.py classify --image leaf.jpg --crop tomato
python3 scripts/agri.py treat --classification classification.json
python3 scripts/agri.py localize --text advisory.txt --lang hi
```

## Non-negotiables

- **No remedy below 0.6 confidence.** Not as a possibility, not in a footnote.
- **No disease named without a model.** A filename is not evidence.
- **No specific pesticide dose** you cannot source from a label — say "per label
  rate" and point at the extension officer.
- **No fake translation.** `translated: false` is a correct, useful answer.
- **No promise of an outcome.** "Usually controls X if applied at first sign", not
  "will cure it".
- **Always the disclaimer**: variety, soil and local pesticide rules differ, and a
  local extension officer overrides this pack.

## Done when

A classification exists with a real confidence, the gate was applied (remedies or
an explicit refusal), and the advisory is either translated with product names and
numbers intact or flagged untranslated.
