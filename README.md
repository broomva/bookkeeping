# bookkeeping

Universal knowledge engine for the Broomva bstack. Implements the **LLM Wiki pattern** (Karpathy): instead of dumping everything into a RAG vector store and hoping retrieval works, bookkeeping builds a structured, wikilinked entity graph where every promoted item has a permanent home, a quality score, and traceable provenance. Raw sources flow in through a 7-stage pipeline — they get scored by a two-pass Nous gate, scattered into entity concepts, deduplicated against the existing graph, and promoted to permanent Layer 3 entity pages. Clusters of related entities surface synthesis note candidates. The result is a knowledge graph that compounds across sessions rather than accumulating noise.

---

## Quick Start

```bash
# 1. Ingest a raw source (social run log, transcript, research notes)
python3 scripts/bookkeeping.py ingest --source research/notes/2026-04-05-social-raw.md

# 2. Run the full pipeline (score → scatter → resolve → promote → synthesize → lint)
python3 scripts/bookkeeping.py run

# 3. Check graph health and pending synthesis candidates
python3 scripts/bookkeeping.py status

# 4. (Optional) Project a Layer-4 synthesis MD to single-file HTML for human reading
python3 scripts/bookkeeping.py render <path>     # project MD → single-file HTML (P17)
```

---

## 4-Layer Knowledge Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1 — Ephemeral                                 (never stored) │
│                                                                      │
│  Social threads · passing ideas · unprocessed conversation          │
│  fragments · fleeting observations. Lives only in context windows.  │
│  Discarded when session ends. Nothing is written.                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ INGEST (bookkeeping run)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 2 — Raw Extracts            research/notes/YYYY-MM-DD-*.md  │
│                                                                      │
│  Ingested + scored items. Items scoring 3–4 rest here pending       │
│  review or re-scoring on the next run. Items ≤2 are discarded.      │
│  Items ≥5 are promoted immediately to Layer 3.                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ PROMOTE (score ≥5)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 3 — Entity Pages    research/entities/{type}/{slug}.md       │
│                                                                      │
│  The permanent knowledge graph. Structured, wikilinked, query-able. │
│  One file per concept: tool · person · concept · project · paper ·  │
│  pattern · dataset. Deduplicated on slug + fuzzy title match.       │
│  Source of truth for the Obsidian vault.                            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ SYNTHESIZE (cluster ≥3 entities)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 4 — Synthesis Notes     research/notes/YYYY-MM-DD-*-synth.md │
│                                                                      │
│  Cluster-level understanding. Written when 3+ entities share tags   │
│  or wikilinks. Blog post candidates and architectural decisions      │
│  live here. Authored by human or agent from synthesis candidates.   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 7-Stage Pipeline

1. **INGEST** — Normalize raw sources (JSONL logs, transcripts, web clips, notes) to canonical `{source_id, type, content, timestamp, metadata}` records
2. **SCORE** — Two-pass Nous gate: heuristic fast-path for clear cases (≤2 discard, ≥7 promote), then LLM-as-judge for ambiguous band (3–6) scoring novelty + specificity + relevance (0–3 each)
3. **SCATTER** — Extract 0–5 candidate entity concepts per source item; one source produces N entity candidates
4. **RESOLVE** — Deduplicate candidates against existing graph by exact slug match then fuzzy title match (cutoff 0.80); update existing or create new
5. **PROMOTE** — Write items scoring ≥5 to `research/entities/{type}/{slug}.md`; hold 3–4 in Layer 2; discard ≤2
6. **SYNTHESIZE** — Detect entity clusters (3+ entities sharing tags or wikilinks); flag clusters without synthesis notes as pending candidates
7. **LINT** — Validate all entity pages: `core_claim` ≤140 chars, sources present, `related` uses `[[wikilink]]` format, no broken wikilinks; output lint report

---

### `bookkeeping render` — Category B projection (MD → HTML)

Project a Layer 4 synthesis MD to a single-file HTML for human reading.

```bash
bookkeeping render <path>             # one file
bookkeeping render research/notes/    # directory glob (*-synthesis.md)
bookkeeping render --layer 4          # all Layer 4 synthesis notes
bookkeeping render --link-html        # rewrite [[slug]] → .html targets
```

The HTML is gitignored by default (regenerable from MD) and carries
`canonical:` frontmatter pointing back to its source MD. See
[SKILL.md "Format Discernment (P17)"](SKILL.md#format-discernment-p17)
for when to use this vs keep MD-only.

---

## Directory Structure

```
skills/bookkeeping/
├── SKILL.md                    # Primary skill definition — authoritative source for all thresholds
├── README.md                   # This file — practical overview
│
├── references/
│   ├── scoring-rubric.md       # Full Nous gate rubric with score examples (defers to SKILL.md)
│   ├── entity-schema.md        # Entity page field spec and valid type/status values
│   └── promotion-workflow.md   # Layer definitions and promotion decision tree
│
├── scripts/
│   └── bookkeeping.py          # Main CLI: run / ingest / score / promote / synthesize / lint / status / query
│
└── templates/
    └── entity-page.md          # Canonical template for new Layer 3 entity pages
```

Output locations (outside the skill directory):

```
research/
├── notes/
│   ├── YYYY-MM-DD-{source}-raw.md        # Layer 2 raw extracts
│   └── YYYY-MM-DD-{topic}-synthesis.md   # Layer 4 synthesis notes
└── entities/
    ├── tool/{slug}.md
    ├── person/{slug}.md
    ├── concept/{slug}.md
    ├── project/{slug}.md
    ├── paper/{slug}.md
    ├── pattern/{slug}.md
    └── dataset/{slug}.md

~/.config/bookkeeping/
├── run-log.jsonl               # Append-only log of all pipeline runs
└── status.json                 # Current graph stats + lint errors + pending synthesis candidates
```

---

## Integration with Other bstack Skills

| Skill | How it delegates to bookkeeping |
|-------|---------------------------------|
| `social-intelligence` | Delegates Phase 2 (Knowledge Extraction Loop) entirely. After each engagement run, passes `loop-log.jsonl` to `bookkeeping run`. Bookkeeping owns scoring, promotion, and entity creation for all social content. |
| `knowledge-graph-memory` | Receives promoted entity page paths from bookkeeping. Indexes them into the Obsidian vault via the `research/entities/` → `~/broomva-vault/08-Research/entities/` symlink. |
| `content-creation` | Consumes blog candidate flags from `~/.config/bookkeeping/status.json` under `pending_synthesis`. Entity wikilinks become source material for blog posts and multimedia assets. |
| `deep-dive-research` | Outputs raw research logs that bookkeeping ingests as structured sources. Ensures every research session feeds the permanent entity graph rather than disappearing into a transcript. |
| `bstack P8` | This skill is the 8th core automation primitive. All knowledge-producing sessions are expected to run bookkeeping before closing, completing the feedback loop into the governance layer. |

---

## Key Design Decisions

### Two-Pass Scoring: Why Not Just LLM Every Item?

The heuristic fast-path handles the extremes (clear noise at ≤2, clear signal at ≥7) without an LLM call. In a high-volume social engagement session this can filter 70–80% of items cheaply. The LLM judge is reserved for the genuinely ambiguous middle band (3–6) where a reasoning pass adds real value. This keeps latency and cost proportional to uncertainty, not volume.

### Scatter vs Accumulate: Why Entity-First?

Most knowledge tools accumulate notes that grow linearly and become unsearchable. The scatter approach forces decomposition: every source must produce discrete entity candidates before anything is written. This means the graph stays navigable — you find `karpathy-llm-wiki-pattern` directly rather than hoping a keyword search surfaces it from a buried note. The cost is discipline at ingest time; the payoff is a permanently query-able graph.

### Entity-First vs Note-First: Why Not Just Keep Notes?

Synthesis notes (Layer 4) are the output of the knowledge process, not the storage format. Entity pages (Layer 3) are the atoms. When you want to understand a concept, you query the entity page directly. When you want to understand a theme or pattern, synthesis notes pull from entity clusters. Keeping notes as the primary format conflates storage with synthesis and makes the graph impossible to maintain at scale. Entity pages are small, typed, and deduplication-safe.

---

## Reference Files

- `references/scoring-rubric.md` — Full Nous gate rubric with worked examples for each score level (0–3) per dimension
- `references/entity-schema.md` — Complete entity page schema: all required/optional fields, valid type values, valid status values
- `references/promotion-workflow.md` — Layer definitions, promotion decision tree, status transitions from `candidate` → `active` → `archived`
- `templates/entity-page.md` — Canonical YAML front-matter + body template for new entity pages
