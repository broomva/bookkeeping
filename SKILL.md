---
name: bookkeeping
version: 1.0.0
primitive: P8
description: Universal knowledge engine — scores, promotes, and compounds knowledge across all sources into a permanent, query-able entity graph
author: broomva
tags:
  - knowledge-graph
  - knowledge-extraction
  - scoring
  - entity-graph
  - bstack
  - p8
compounding:
  - social-intelligence
  - knowledge-graph-memory
  - content-creation
  - deep-dive-research
---

# bookkeeping — Universal Knowledge Engine

The bookkeeping skill is **bstack primitive P8**: the universal knowledge bookkeeping layer that sits beneath every knowledge-producing workflow in the Broomva stack. It implements the LLM Wiki pattern (Karpathy): raw sources flow in, get scored, scatter into entity pages, deduplicate against the existing graph, and compound into synthesis notes. Every other skill that produces knowledge delegates its extraction and promotion phases here.

---

## When to Invoke

- After any knowledge-gathering session: social engagement runs, research experiments, deep-dive sessions, conversation transcripts
- When prompted with `/bookkeeping` or `bookkeeping run`
- Automatically after the `social-intelligence` loop runs (Phase 2 — Knowledge Extraction — is fully delegated here)
- Before creating synthesis notes or flagging blog post candidates
- When asked to "extract knowledge from", "distill", "index", or "promote" any content
- When entity pages are stale or lint errors are detected in the entity graph

---

## Pipeline — 7 Stages

Each stage is idempotent. Stages can be run individually or as a full pipeline via `bookkeeping run`.

### Stage 1 — INGEST

Load raw sources from any of: JSONL run logs, conversation transcripts (Markdown), web clips, manual notes, social engagement logs. Normalize every item to the canonical source record:

```json
{
  "source_id": "sha256-prefix-8chars",
  "type": "social_comment | transcript | web_clip | note | experiment_log",
  "content": "...",
  "timestamp": "ISO-8601",
  "metadata": {
    "origin": "moltbook | x | conversation | web | manual",
    "author": "...",
    "url": "...",
    "session_id": "..."
  }
}
```

All ingested records are appended to the Layer 2 raw extract file at `research/notes/YYYY-MM-DD-{source}-raw.md` and to `~/.config/bookkeeping/run-log.jsonl`.

### Stage 2 — SCORE

Two-pass scoring against the Nous gate rubric (full spec in `references/scoring-rubric.md`):

**Dimensions** (each 0–3):
- `novelty` — Is this genuinely new to the knowledge graph?
- `specificity` — Is this concrete and actionable, not generic?
- `relevance` — Does this connect to active projects, research threads, or strategic concerns?

**Heuristic fast-path** (no LLM call needed):
- Score ≤ 2 → discard immediately (clearly low-signal)
- Score ≥ 7 → promote immediately (clearly high-signal)

**LLM-as-judge** for ambiguous band (score 3–6):
- Pass item + existing entity graph context to judge (see LLM Judge Spec below)
- Output: per-item score tuple `(novelty, specificity, relevance)` + total + promote flag + candidate entity slugs

Scoring output is written to the raw extract file as a YAML front-matter annotation per item.

### Stage 3 — SCATTER

From each high-scoring source item, extract N candidate entity concepts (0–5 per source). Each candidate becomes a potential entity page in the graph. Scatter means one source can produce multiple entities — a single research thread might yield a tool entity, a person entity, a technique entity, and a project entity.

Candidates are output as slug strings (lowercase, hyphen-separated): `e.g. "bitnet-ternary-weights", "karpathy-llm-wiki-pattern"`.

### Stage 4 — RESOLVE

Deduplicate candidates against the existing entity graph:

1. **Exact wikilink slug match** — check `research/entities/{type}/{slug}.md` directly
2. **Fuzzy title match** — compare candidate title against all existing entity titles (cutoff: 0.80 similarity). If match found → update existing entity. If no match → create new entity.

Resolution prevents graph fragmentation. A single concept must not appear under multiple slugs.

### Stage 5 — PROMOTE

Apply promotion decision based on total score:

| Score | Action | Destination |
|-------|--------|-------------|
| ≥ 5   | Promote | `research/entities/{type}/{slug}.md` (Layer 3) |
| 3–4   | Hold    | Stays in `research/notes/YYYY-MM-DD-{source}-raw.md` (Layer 2) |
| ≤ 2   | Discard | Dropped, not written |

Entity page type is inferred from the candidate context: `tool`, `person`, `concept`, `project`, `paper`, `pattern`, `dataset`. Use the template at `templates/entity-page.md` when creating new pages.

### Stage 6 — SYNTHESIZE

After promotion, scan the entity graph for clusters: groups of 3 or more entities that share tags or reference each other via `[[wikilinks]]`. For each cluster:

1. Check if a synthesis note already exists in `research/notes/` covering that cluster
2. If not → flag the cluster as a synthesis candidate with a suggested filename: `YYYY-MM-DD-{cluster-topic}-synthesis.md`
3. Synthesis candidates are written to `~/.config/bookkeeping/status.json` under `pending_synthesis`

Synthesis notes are not auto-generated — they are flagged for human or agent authorship. The bookkeeping skill creates the scaffold, not the prose.

### Stage 7 — LINT

Validate all entity pages in `research/entities/` against the schema (full spec in `references/entity-schema.md`):

- `core_claim` field present and ≤ 140 characters
- `sources` field present and non-empty
- `related` field uses `[[wikilink]]` format (not bare URLs or plain text)
- No broken wikilinks (all `[[slug]]` references resolve to existing entity files)
- `status` field is one of: `active`, `archived`, `stub`, `candidate`
- `type` field is one of: `tool`, `person`, `concept`, `project`, `paper`, `pattern`, `dataset`

Lint report is written to stdout and to `~/.config/bookkeeping/status.json` under `lint_errors`. A non-zero lint error count does NOT block the pipeline — it surfaces warnings only.

---

## Self-Maintenance Rules (CRITICAL)

These rules govern any agent that modifies files in this skill. They are enforced by reasoning, not by hooks. When you touch any file under `skills/bookkeeping/`, you MUST apply these rules before completing the task.

**Rule 1 — Stage count consistency**
When adding, removing, or renaming a pipeline stage: update the stage count and stage list in BOTH this file AND `README.md`. The stage count in both files must always match.

**Rule 2 — Scoring threshold consistency**
When changing the promote threshold (currently ≥5), the discard threshold (currently ≤2), or the heuristic fast-path boundaries (currently ≤2 / ≥7): update BOTH this file AND `references/scoring-rubric.md`. The two files must always agree on all threshold values.

**Rule 3 — Entity schema consistency**
When adding a new entity `type` value or a new `status` value: update BOTH `references/entity-schema.md` AND `templates/entity-page.md`. The template must always reflect all valid field values defined in the schema.

**Rule 4 — Layer definition consistency**
When changing the layer count (currently 4) or redefining layer boundaries: update BOTH this file AND `references/promotion-workflow.md`. All destination path patterns must be consistent across both files.

**Rule 5 — Post-modification verification**
After any modification to any file in this skill, run:
```bash
python3 scripts/bookkeeping.py lint --all
python3 scripts/bookkeeping.py status
```
Fix all lint errors before considering the task complete.

**Rule 6 — SKILL.md is authoritative**
This SKILL.md is the single source of truth for all thresholds, stage definitions, and layer boundaries. All other files in this skill (references/, templates/, README.md) defer to it. If a conflict exists between this file and any other file, this file wins and the other file must be updated.

---

## CLI Reference

```bash
python3 scripts/bookkeeping.py run                    # Full 7-stage pipeline
python3 scripts/bookkeeping.py ingest --source FILE   # Ingest single file
python3 scripts/bookkeeping.py score --file FILE      # Score items in raw extract
python3 scripts/bookkeeping.py promote --file FILE    # Promote pending items
python3 scripts/bookkeeping.py synthesize             # Detect clusters, flag candidates
python3 scripts/bookkeeping.py lint --all             # Validate all entity pages
python3 scripts/bookkeeping.py status                 # Show knowledge graph stats
python3 scripts/bookkeeping.py query "concept-slug"   # Find and display entity page
```

All commands accept `--dry-run` to preview changes without writing. All commands write structured output to `~/.config/bookkeeping/run-log.jsonl`.

---

## 4-Layer Knowledge Lifecycle

```
Layer 1 — Ephemeral (never stored)
  Social threads, passing ideas, unprocessed conversation fragments.
  Lives only in context windows. Discarded after session.

Layer 2 — Raw Extracts  research/notes/YYYY-MM-DD-{source}-raw.md
  Ingested + scored items. Score 3-4 items rest here.
  Reviewed manually or swept by next bookkeeping run.

Layer 3 — Entity Pages  research/entities/{type}/{slug}.md
  Promoted items (score ≥5). Structured, query-able, wikilinked.
  The permanent knowledge graph. Source of truth for the vault.

Layer 4 — Synthesis Notes  research/notes/YYYY-MM-DD-{topic}-synthesis.md
  Cluster-level understanding. Written when ≥3 entities share a theme.
  Blog candidates and architectural decisions live here.
```

---

## Output Locations

| Output | Path |
|--------|------|
| Layer 2 raw extracts | `research/notes/YYYY-MM-DD-{source}-raw.md` |
| Layer 3 entity pages | `research/entities/{type}/{slug}.md` |
| Layer 4 synthesis notes | `research/notes/YYYY-MM-DD-{topic}-synthesis.md` |
| Run log (JSONL) | `~/.config/bookkeeping/run-log.jsonl` |
| Status + lint report | `~/.config/bookkeeping/status.json` |

---

## Integration Points

| Skill | Integration |
|-------|-------------|
| `social-intelligence` | Delegates Phase 2 (Knowledge Extraction Loop) entirely to bookkeeping. After each engagement run, calls `bookkeeping run` on the loop-log.jsonl. |
| `knowledge-graph-memory` | Receives entity page paths after promotion. Indexes them into the Obsidian vault via the symlink layer. |
| `content-creation` | Receives blog candidate flags from `status.json` → `pending_synthesis`. Picks up entity wikilinks as source material. |
| `deep-dive-research` | Outputs raw research logs that bookkeeping ingests. Ensures research sessions feed the permanent entity graph, not just the conversation transcript. |
| `CLAUDE.md P8` | This skill is bstack primitive P8. Listed alongside P1–P7 in the Bstack Core Automation Primitives table. All sessions that produce knowledge are expected to run bookkeeping before closing. |

---

## LLM Judge Spec

Used in Stage 2 for the ambiguous band (score 3–6). The judge is called with the item text and a snapshot of relevant existing entities.

**System prompt:**
```
You are a knowledge quality evaluator for a personal knowledge OS (the Broomva bstack).
Your job is to score extracted knowledge items on three dimensions and decide whether to
promote them into the permanent entity graph.

Scoring dimensions (each 0–3):
  novelty      — 0: already well-represented in graph. 3: genuinely new concept or framing.
  specificity  — 0: vague, generic, or obvious. 3: concrete, named, actionable.
  relevance    — 0: unrelated to active projects or research threads. 3: directly applicable.

Promotion threshold: total ≥ 5 → promote = true.

Output ONLY valid JSON. No markdown fences, no explanation outside the JSON object.
```

**User prompt template:**
```
ITEM TEXT:
{item_content}

EXISTING ENTITY GRAPH CONTEXT (relevant excerpts):
{entity_context}

ACTIVE PROJECT TAGS FOR RELEVANCE SCORING:
{active_tags}

Score this item and identify candidate entity slugs it could produce.

Output format:
{
  "novelty": <0-3>,
  "novelty_reason": "<one sentence>",
  "specificity": <0-3>,
  "specificity_reason": "<one sentence>",
  "relevance": <0-3>,
  "relevance_reason": "<one sentence>",
  "total": <0-9>,
  "promote": <true|false>,
  "candidate_entities": ["slug-one", "slug-two"]
}
```

---

## Reference Files

| File | Purpose |
|------|---------|
| `references/scoring-rubric.md` | Full Nous gate rubric with examples for each score level |
| `references/entity-schema.md` | Complete entity page schema with all valid field values |
| `references/promotion-workflow.md` | Layer definitions, promotion decision tree, status transitions |
| `templates/entity-page.md` | Canonical template for new entity pages |
| `scripts/bookkeeping.py` | Main CLI implementation |
