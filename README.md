# Setu

**सेतु** — *bridge.*

A multi-domain agentic framework for Claude Code, Cursor and Codex. One router,
six domain packs, 35 sub-agents, and a shared memory layer that makes every pack
slightly better at the thing you keep asking it to do.

```bash
./install.sh                                   # Claude Code, all six packs
./install.sh --target cursor                   # .cursor/rules/
./install.sh --target codex                    # writes a managed block in AGENTS.md
./install.sh --profile minimal --domains cyber,finance
./install.sh --uninstall --target claude       # removes exactly what it wrote
```

---

## Why this exists

Most agent frameworks give you one clever assistant that is mediocre at
everything. Setu gives you six focused packs with different failure modes, each
with a gate that stops it doing the expensive kind of wrong:

| Pack | Gate that actually blocks |
|---|---|
| **cyber** | no active testing without confirmed authorization; SSRF guard on its own tooling |
| **webdev** | no fix accepted without a green test suite; a deterministic design critic rejects generic AI-site patterns |
| **admin** | never marks a document final — a named human must confirm |
| **finance** | never lets a language model produce a number; EMI maths is Python only |
| **agri** | no treatment advice below 0.6 classification confidence |
| **education** | study planning is deterministic; the coach supports learning rather than completing graded work |

Those gates are in **code**, not in prose. A rule that lives only in a prompt gets
rationalised away on the third retry; a rule that lives in a function returns a
refusal. `python3 scripts/setu.py selftest` asserts the code-enforced gates still hold.

---

## Architecture

```
                    ┌──────────────────────────┐
                    │   SETU ORCHESTRATOR      │  routes user request
                    │   skills/setu-orchestrator│  to the right domain
                    └─────────────┬────────────┘
                                  │
      ┌──────────┬────────────────┼────────────────┬──────────┐
      ▼          ▼                ▼                ▼          ▼          ▼
  ┌───────┐  ┌───────┐        ┌───────┐        ┌───────┐  ┌───────┐  ┌─────────┐
  │ CYBER │  │WEBDEV │        │ AGRI  │        │ ADMIN │  │FINANCE│  │EDUCATION│
  │ 20    │  │ 5     │        │ 3     │        │ 3     │  │ 3     │  │ 1       │
  │agents │  │agents │        │agents │        │agents │  │agents │  │ agents  │
  └───┬───┘  └───┬───┘        └───┬───┘        └───┬───┘  └───┬───┘  └────┬────┘
      │          │                │                │          │           │
      └──────────┴────────────────┴────────────────┴──────────┴───────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │   SHARED MEMORY LAYER    │  scripts/memory.py
                    │   - feedback log         │  JSON, stateless
                    │   - few-shot retrieval   │  read → rank → write
                    │   - per-pack usage stats │
                    └──────────────────────────┘
                                  │
                    ┌──────────────────────────┐
                    │   MODEL WRAPPER          │  scripts/model_wrapper.py
                    │   claude · gpt · ollama  │  SETU_MODEL env var
                    └──────────────────────────┘
```

### What "shared" actually means

Three things are genuinely shared, not copied per pack:

1. **`pattern_engine.py` + `rules/patterns.json`** — `cyber-injection` and
   `finance-scam-check` call the *same* matcher with the *same* probabilistic-OR
   scorer. Add a scam rule once and both packs get it. "Confidence 0.8" means the
   same thing in both.
2. **`memory.py`** — every pack logs outcomes and retrieves few-shot examples
   through one file format, so `setu.py stats` can compare packs.
3. **`model_wrapper.py`** — agent frontmatter says `model: haiku` / `model: opus`
   (a *tier*, not a vendor model). `resolve_model` maps the tier onto whatever
   `SETU_MODEL` selects, so the token-spend design survives a switch to GPT or a
   local Ollama model without touching the agent files.

---

## The six packs

### 1 · Cyber — 20 agents

```
request → cyber-orchestrator-router (picks the minimum set, never all 20)
  → recon: subdomain-enum · tech-fingerprint · endpoint-discovery · osint   [haiku]
  → specialists (only those that apply)                                     [opus]
      injection · access-control · auth-flaws · xss-csrf · ssrf ·
      business-logic · api-security · cloud-misconfig · crypto-weakness ·
      dependency-scan
  → triage: severity-scorer (CVSS 3.1) → dedup                              [opus]
  → reporting: impact-writer → poc-writer → report-formatter                [opus]
  → memory.log_feedback(pack="cyber", …)
```

```bash
python3 scripts/cyber_chain.py --target "https://lab.internal/item?id=1" \
    --i-have-authorization --allow-private --out-dir runs/run-001
```

**Two gates, enforced in `cyber_recon.py`:**

- `assert_authorized` refuses active testing without `--i-have-authorization`.
  `--passive` (DNS + certificate transparency) is always allowed because it sends
  nothing to the target.
- `assert_public` blocks targets that resolve to loopback, private, link-local or
  metadata ranges — checked *after* DNS resolution, so a friendly public hostname
  pointing at `127.0.0.1` is still caught. A recon tool that happily fetches
  `http://169.254.169.254/` is a vulnerability with a CLI.

**Why the CVSS maths is real code:** `severity_scorer.py` implements the v3.1
base-score algorithm including the spec's round-up function (not `round()`), and
ships a `--selftest` against reference vectors. A severity number a triager will
argue with must be arithmetic they can reproduce, not something a model felt.

`--target cursor` users note: recon, injection testing and the chain work
identically; only the skill file location differs.

### 2 · Web Dev — 5 agents, two paths

```
BUILD:  request → webdev-builder (decides palette + type + layout, in writing)
                → webdev-design-critic (fresh context, runs design_check.py)
                → APPROVE | REJECT (max 2 loops, then escalate to a human)

FIX:    error → webdev-diagnose → webdev-fix → webdev-verify
                (localise)      (unified diff)  (snapshot → apply → run tests →
                                                 ACCEPT or revert byte-for-byte)
```

**The design critic is a linter, not a vibe check.** `design_check.py` implements
the five generic-AI-site tells deterministically:

| Rule | How it is detected |
|---|---|
| `gradient-purple-blue` | stops converted to HSL; hue 210–300 with real saturation |
| `font-generic` | only Inter / system-ui / Roboto etc., with no second family |
| `template-structure` | hero **and** exactly 3 cards **and** footer **and** centred text |
| `emoji-as-icon` | emoji inside a link, button, nav item or list item |
| `placeholder-copy` | lorem ipsum, "Feature One", "Your headline here", generic superlatives |

Proof it is not a rubber stamp — a deliberately generic page scores `REJECT` with
5 blocking violations, and `examples/webdev/grain-store.html` (a grain co-op
ledger: kraft/crust palette, Fraunces + Public Sans, rail + table layout) scores
`APPROVE 100`.

**The fix path refuses to accept a bad patch.** `webdev-verify` snapshots the
files, applies the diff, runs the suite, and reverts on failure. Verified: a patch
that silences a `KeyError` by dropping the value → `REVERTED`; the correct fix →
`ACCEPTED` with `3 passed`.

> **Provenance note.** The brief asked for `webdev-builder` to inherit
> `/mnt/skills/public/frontend-design/SKILL.md`. **That path does not exist in the
> build environment** (there is no `/mnt/skills` mount), so it could not be
> inherited. Rather than silently assume, the design doctrine was written into
> `webdev-builder.md` directly and — more importantly — the critic enforces it in
> code. If you have that skill available, drop its guidance into
> `webdev-builder.md`; the linter will keep the critic honest either way.

### 3 · Admin / Legal — 3 agents

```
request → admin-draft ── document + [[NEEDS: …]] for every missing fact
        → admin-verify-law ── verbatim / paraphrase / not-in-corpus / unknown-act
        → admin-format ────── score + hazards
        → ══ HUMAN CONFIRMATION GATE (--confirmed-by "<name>" --yes) ══
        → final document with a banner naming who confirmed it and when
```

**Quote verbatim, or don't quote.** `verify_quotes` compares each quotation
against a local corpus; a paraphrase *inside quotation marks* is `BLOCKING`,
because a model-written legal document's most likely failure is a plausible
paraphrase presented as statute.

**"Not in my corpus" is never "does not exist."** Four distinct statuses —
`verified-verbatim`, `not-in-corpus`, `unknown-act`, `suspicious-section` — and
only a *wrong quotation* blocks. A section cited as a legal basis with no quote is
correct drafting, not an error, so it is advisory.

**No auto-finalize.** `admin_finalize` refuses without a confirmation flag, a
named human, no remaining `[[NEEDS: …]]`, verify-law not `BLOCK`, and format not
`FAIL`. There is no code path from draft to final that an agent can take alone.

### 4 · Finance — 3 agents

```
expense → finance-categorize ── category + why + confidence (uncategorised is valid)
message → finance-scam-check ── shared pattern_engine + risk band + what to do now
loan    → finance-emi-calc ──── Decimal maths + reconciled schedule
                                └─ model explains the finished numbers, nothing else
```

**No model-generated numbers.** `finance.emi` uses `Decimal`, rounds to the rupee
with half-to-even, and builds a schedule that absorbs the rounding residue so the
closing balance is exactly `0.00`. A second guard in `call_model` **refuses
math-shaped prompts by default** and raises rather than guessing.

Verified: 500,000 @ 9.2% / 60m → `10,428` (matches the closed-form reference in
four independent cases), total interest `125,663.18`, `reconciled: true`,
principal parts summing to exactly 500,000.

**Scam screening reuses the cyber rules** — one `patterns.json`, one scorer, so a
pattern added for either pack improves both. The best negative verdict is
`no-pattern-matched`, never "safe".

### 5 · Agriculture — 3 agents

```
image → agri-classify ── label + confidence (or "unidentified"; never invented)
      → ══ GATE 0.6 ══ below → "uncertain, consult an expert", zero remedies
      → agri-treat ────── remedies ordered cultural → biological → chemical
      → agri-localize ─── 13 Indian languages, product names and numbers preserved
```

Agri includes an opt-in **plant-content validation gate** backed by the pinned
Apache-2.0 Kindwise `router.tiny` model. Install `requirements-agri-image.txt`
and run `python scripts/agri.py classify --image leaf.jpg --crop tomato
--validate-plant`; model weights are fetched only when that explicit flag is
used. The gate blocks classification when validation fails; it does not diagnose
disease, and is a router rather than a calibrated field diagnostic. Disease
classification remains a separate concern. Without the flag, legacy code callers
retain compatibility; upload workflows that accept untrusted user files should
explicitly require validation before classification or treatment. Translation
remains callable-backed and reports untranslated text honestly when unavailable.

Verified boundary: `0.599 → treatment_allowed: false`, `0.60 → true`. Two *distinct*
refusal paths — low confidence (`uncertain`) and a confident label outside the
knowledge base (`no-guidance`) — because reporting "below the 0.60 threshold" at
confidence 0.95 would be a self-contradiction.

---

## Shared memory layer

`scripts/memory.py` — deliberately cheap. No fine-tuning, no vector database, no
daemon. Every operation is read-file → compute → write-file, so the process can be
a fresh interpreter every time.

```bash
python3 scripts/memory.py log --pack cyber --input "enumerate subs for app.example.com" \
    --output "14 live hosts, 2 private-range" --helpful true
python3 scripts/memory.py few-shot --pack cyber --n 3 --query "enumerate subs for shop.example.com"
python3 scripts/memory.py scores        # per-pack helpfulness, flags weak packs
```

**Relevance** is a dependency-free hybrid: IDF-weighted token overlap × exponential
recency decay (45-day half-life) × helpfulness prior. Unhelpful records are
excluded by default — we do not teach the model to repeat a failure.

The wiring seam is `setu.py context`, which turns examples into an injection
prompt:

```bash
python3 scripts/setu.py context --pack cyber --request "enumerate subs for shop.example.com"
# → {"examples_found": 1, "injection_prompt": "You are running Setu's cyber pack. …"}
```

Storage is atomic (temp file + rename) and capped (`SETU_MEMORY_MAX`, default
2000 entries), so a crash mid-write cannot corrupt the log.

---

## Model wrapper

```python
from model_wrapper import call_model
text = call_model("summarise these findings", model="claude")
```

| `SETU_MODEL` | Backend |
|---|---|
| `claude` (default), `claude-haiku`, `-sonnet`, `-opus` | Anthropic |
| `gpt`, `gpt-4o`, `gpt-mini` | OpenAI |
| `ollama:qwen2.5-coder:7b` | local Ollama |

- **No hard dependency** — SDKs and HTTP calls are lazily reached, and a missing
  key raises `ModelConfigError`, not an `ImportError` at import time. A
  `--target cursor` install works on a machine with no SDKs at all.
- **Deterministic offline fallback** — `SETU_OFFLINE=1` routes to a clearly
  labelled `[offline-stub]` so every script can be exercised in CI with no network
  and no spend. It never pretends to be a model.
- **Math guard** — refuses math-shaped prompts unless `allow_math=True`. That is
  the belt to `finance-emi-calc`'s braces.

---

### 6 · Education — study planning and learner coaching

```text
request → education-study-coach ── questions, explanations, and original practice
study inputs → education.py ─────── deterministic learning sessions + 1/3/7-day reviews
```

The study planner accepts topics, a start date, exam date, and daily time budget. It
keeps the exam date free, optionally skips weekends, and lists any topics or reviews it
cannot fit. It does not claim that completing scheduled sessions proves mastery. The
skill instructions also set a firm academic-integrity boundary: coach, explain, or give
feedback, but never complete graded work for the learner.

```bash
python3 scripts/education.py plan \
  --topics "fractions,decimals,word problems" \
  --start-date 2026-06-01 --exam-date 2026-06-15 \
  --minutes-per-day 90
```

## Install & uninstall

```bash
./install.sh                                  # claude, all packs
./install.sh --target cursor                  # .cursor/rules/
./install.sh --target codex                   # managed block in AGENTS.md
./install.sh --profile minimal                # cyber + finance only
./install.sh --domains admin,agri             # choose explicitly
./install.sh --dry-run                        # show, write nothing
./install.sh --list                           # what is installed
./install.sh --uninstall --target claude
```

`install.ps1` is the Windows equivalent with the same flags.

### How uninstall stays exact

Every write is recorded in `~/.setu/install-ledger.json`, split into two kinds:

- **owned files** — created by Setu → deleted on uninstall
- **shared files** — `AGENTS.md` that existed before Setu → uninstall restores the
  `.setu-bak` taken before the edit, or strips only the block between the
  `<!-- BEGIN SETU -->` / `<!-- END SETU -->` markers

That distinction exists because the first version of this installer **deleted a
user's `AGENTS.md` on uninstall**. Uninstall must never be able to cause data loss.

`--profile minimal` installs only what the selected packs need: an
`--domains agri` install is 10 files, with no CVSS scorer and no pattern engine.

### Target layouts

| Target | Skills | Agents | Notes |
|---|---|---|---|
| `claude` | `~/.claude/skills/setu-*/SKILL.md` | `~/.claude/agents/*.md` | real Claude Code plugin layout |
| `cursor` | `.cursor/rules/setu-*.mdc` | `.cursor/rules/<agent>.mdc` | native Cursor rules format |
| `codex` | `./setu/skills/…` + `AGENTS.md` block | `./setu/agents/*.md` | AGENTS.md is edited, never replaced |

---

## What works without an API key

Everything structural, and every gate. This is a design goal, not an accident:

```bash
$ python3 scripts/setu.py selftest
  [PASS] cyber: authorization gate
  [PASS] cyber: SSRF guard
  [PASS] cyber: CVSS v3.1 base score
  [PASS] webdev: design critic rejects generic page
  [PASS] webdev: no green suite means no acceptance
  [PASS] agri: 0.6 confidence gate
  [PASS] admin: human confirmation required
  [PASS] finance: deterministic EMI + reconciled schedule
  [PASS] finance: model cannot compute numbers
  [PASS] memory: log and retrieve

11/11 gates hold
```

```bash
$ python3 -m pytest tests/ -q
103 passed
```

Gates for packs you did not install are reported `SKIP`, not `FAIL`.

---

## Repository layout

```
setu/
├── .claude-plugin/
│   ├── plugin.json                  # plugin manifest (6 skills, agents dir)
│   └── marketplace.json             # single-plugin marketplace
├── skills/
│   ├── setu-orchestrator/SKILL.md   # top-level router + route announcement
│   ├── setu-cyber/SKILL.md          # 20-agent chain + two gates
│   ├── setu-webdev/SKILL.md         # build path + fix path + provenance note
│   ├── setu-agri/SKILL.md           # 0.6 confidence gate
│   ├── setu-admin/SKILL.md          # verbatim citation + human gate
│   ├── setu-finance/SKILL.md        # deterministic maths + disclaimer
│   └── setu-education/SKILL.md     # study planning + learning boundaries
├── agents/                          # 35 sub-agents, one .md each
│   ├── cyber-*.md                   #  20
│   ├── webdev-*.md                  #   5
│   ├── agri-*.md                    #   3
│   ├── admin-*.md                   #   3
│   ├── finance-*.md                 #   3
│   └── education-study-coach.md     #   1
├── scripts/
│   ├── memory.py                    # feedback log + few-shot retrieval
│   ├── model_wrapper.py             # call_model(): claude · gpt · ollama
│   ├── setu.py                      # CLI: doctor · selftest · context · route
│   ├── common.py                    # finding schema, disclaimers, atomic JSON
│   ├── pattern_engine.py            # shared matcher (cyber + finance)
│   ├── rules/
│   │   ├── patterns.json            # 27 rules: sqli/cmdi/traversal/ssti/scam/secrets
│   │   ├── dependencies.json        # 12 offline advisories
│   │   └── legal_corpus.json        # hand-checked provisions, marked not-current
│   ├── cyber_recon.py               # http_get + the two safety gates
│   ├── cyber_scan.py                # specialists + CLI
│   ├── cyber_chain.py               # end-to-end chain runner
│   ├── severity_scorer.py           # CVSS v3.1 + ranking + --selftest
│   ├── dedup.py                     # merge duplicates, keep evidence, median confidence
│   ├── report_formatter.py          # HackerOne/VRP markdown, redacting
│   ├── design_check.py              # the five generic-AI-site tells, as code
│   ├── webdev_fix.py                # diagnose → apply → verify → revert
│   ├── admin_docs.py                # draft · verify-law · format · finalize
│   ├── finance.py                   # EMI (Decimal) · categorize · scam-check
│   ├── education.py                 # deterministic study plan + spaced reviews
│   ├── agri.py                      # classify · treat (0.6 gate) · localize
│   └── install_state.py             # owned-vs-shared ledger for exact uninstall
├── examples/webdev/grain-store.html # a page that passes the design critic (100)
├── tests/
│   ├── fixture_server.py            # deliberately vulnerable local target
│   ├── test_finance.py              # 12 tests, incl. closed-form EMI cross-checks
│   ├── test_agri.py                 # 13 tests, incl. the 0.599/0.60 boundary
│   └── test_education.py            # schedule, budget, CLI, and routing tests
├── install.sh
├── install.ps1
└── README.md
```

---

## Design decisions worth arguing with

- **Gates in code, not prompts.** A prompt-level rule is a suggestion; a function
  that returns an empty `remedies` list is a gate. Every safety property here has
  a `selftest` entry, so removing one breaks the build.
- **Honest negatives over confident guesses.** `uncategorized`, `unidentified`,
  `no-pattern-matched`, `not-in-corpus`. Each is a *useful* answer, and each is
  visibly different from a wrong one.
- **The critic runs in code so it cannot be talked out of it.** A model reviewing
  its sibling's design output rubber-stamps it. A linter that measures hue angles
  does not.
- **Intermediate artifacts on disk.** Each chain stage is a separate process with
  JSON between them. Costs a few file writes; buys re-runnability, contained
  failures, and an audit trail a human reviewer needs to check a claim.
- **Verify reverts.** A patch that turns the suite red is worse than no patch,
  because it hides the bug behind a green-looking commit.
- **Token tiers in frontmatter.** `haiku` for recon, `opus` for triage and
  reporting — the expensive judgement calls get the expensive model, and the
  high-volume lookups do not.

## Limitations

- The cyber advisory database is a 12-entry offline subset, not a CVE feed. Use
  `pip-audit` / `npm audit` / OSV alongside it.
- The legal corpus covers a handful of Indian central statutes and is **not
  current**. It distinguishes "not found" from "does not exist" for exactly this
  reason.
- The agriculture knowledge base has 7 entries. Everything else returns
  `no-guidance` and defers.
- `design_check.py` catches the five named patterns, not every design flaw. It is a
  floor, not a taste engine.
- Scam patterns lag real scams by definition. A negative verdict is not a
  clearance.

## Licence

MIT.
