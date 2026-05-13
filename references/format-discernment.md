# Format Discernment — Reference

Companion to the `Format Discernment (P17)` section in `SKILL.md`. The SKILL
holds the rule; this file holds worked examples, design rationale, and the
observation-period success criteria.

## Why three categories?

Markdown won the LLM-output era because tokens were precious and context
windows tiny. That constraint is gone (May 2026). HTML is now viable for
human-read artifacts because the harness loop is fast enough and context
windows large enough that the extra tokens cost nothing meaningful.

But HTML is not a universal replacement. The agent-substrate use case —
grep, lint, score, score-and-promote, diff-review, line-by-line PR review —
still requires plain text. So the workspace splits into three categories,
not two formats.

## Examples drawn from the workspace

### Category A (MD-only)

```
research/entities/concept/agent-loop-silicon.md
research/notes/2026-05-12-prompt-patterns-raw.md
CLAUDE.md
AGENTS.md
docs/superpowers/specs/2026-05-12-format-discernment-p6-render-design.md
skills/bookkeeping/SKILL.md
```

What they have in common: another agent (or you, with grep) will read this
as text. The artifact's primary consumer is *not* a browser.

### Category B (MD canonical + HTML on demand)

```
research/notes/2026-05-08-egri-calibration-synthesis.md   ← canonical
research/notes/2026-05-08-egri-calibration-synthesis.html ← projected
```

The synthesis is authored, edited, reviewed, and scored as MD. When you
sit down to *read* it (decide whether to publish, share with a colleague,
turn into a blog post), `bookkeeping render` produces the HTML. The HTML
is gitignored — it's a snapshot of a moment, not source-of-truth.

### Category C (HTML-native, frontmatter-carried)

```
research/notes/2026-05-12-consistent-hashing-demo.html
```

This artifact is *intrinsically* interactive — sliders, animated demos,
linked screens. There's no useful MD source: the MD version would lose the
thing that makes the artifact valuable. The HTML carries frontmatter
(HTML-comment YAML) so it can still be a graph member if it deserves one.

## Frontmatter carriers by format

| Format | Carrier | Example |
|--------|---------|---------|
| `.md`, `.markdown` | YAML between `---` lines | `---\ntype: synthesis\n---\n` |
| `.html` | YAML in leading HTML comment | `<!DOCTYPE html>\n<!--\n---\n...\n---\n-->` |
| `.ipynb` | Notebook `metadata` key | `"metadata": { "type": "synthesis", ... }` |
| Binaries (PDF, PNG, …) | Sidecar `.meta.yaml` | `foo.pdf` + `foo.pdf.meta.yaml` |

All carriers must encode at least: `type`, `slug`. Layer 4 artifacts also
encode: `score`, `status`, `source_extracts`, `related_entities`, and (for
projections) `canonical`.

## Wikilink carriers by format

| Format | Carrier | Edge typing |
|--------|---------|-------------|
| `.md` | `[[type/slug]]`, optional `\|alias` | implicit "references" (no edge typing) |
| `.html` | `<a href="../type/slug.md" data-relation="…">alias</a>` | explicit via `data-relation` |

When `bookkeeping render` projects MD → HTML, `[[type/slug]]` becomes
`<a href="../type/slug.md" data-relation="references">slug</a>` (or `.html`
target with `--link-html`). The `data-relation` defaults to `references`
since MD has no edge typing.

## Observation period

For 30 days after the format-discernment rule lands (PR-merge date), the
workspace tracks:

- Number of `bookkeeping render` invocations
- HTML projections actually opened (manual log entry)
- Format-discernment lint warnings/errors fired in CI
- Category-C native artifacts created organically (any `.html` under
  `research/notes/` not produced by `render`)

If, after 30 days:

- ≥10 Category-B renders consumed across ≥5 distinct sessions, AND
- ≥3 Category-C native artifacts emerged organically

…the format-discernment rule earns crystallization into P17 (Format
Discernment Discipline) — the primitive count moves from 16 → 17 and the
P17 row joins the bstack primitives table in `CLAUDE.md` / `AGENTS.md`.

If neither threshold hits, the rule stays where it is (skill-level
reference). If only one hits, revisit at 60 days.

## Why not just emit both formats every time?

That was the obvious naive answer and it's wrong:

1. **Wastes work.** Most artifacts don't benefit from HTML; the projection
   adds storage, review surface, and confusion.
2. **Conflates categories.** Category A artifacts have no HTML twin; Category
   C artifacts have no MD twin. Only Category B has both.
3. **Burns L3 stability budget.** A workspace-wide "emit both" rule is a
   reflexive discipline change; L3 (governance) has λ₃ ≈ 0.006 in the
   composite stability calculation. Reversible single-category rules are
   cheaper.

The three-category test fits in a sentence each. The agent applies it the
same way it currently picks JSON vs YAML vs TOML for any output.
