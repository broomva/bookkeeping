#!/usr/bin/env python3
"""
bookkeeping.py — Broomva knowledge engine (bstack P8)
7-stage pipeline: Ingest → Score → Scatter → Resolve → Promote → Synthesize → Lint

Usage: python3 scripts/bookkeeping.py <command> [options]

Commands:
  run          Full 7-stage pipeline
  ingest       Normalize a single file to internal representation
  score        Score all items in a raw extract file
  promote      Promote pending items (score ≥5) to entity pages
  synthesize   Detect entity clusters, flag synthesis candidates
  lint         Validate entity pages
  status       Print knowledge graph stats
  query        Find and display an entity page
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ensure scripts/ is importable when bookkeeping.py is run as a script
# (so `from render import …` resolves to scripts/render.py).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from render import render_markdown_to_html  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

BROOMVA_ROOT = Path.home() / "broomva"
ENTITIES_DIR = BROOMVA_ROOT / "research" / "entities"
NOTES_DIR = BROOMVA_ROOT / "research" / "notes"
CONFIG_DIR = Path.home() / ".config" / "bookkeeping"
RUN_LOG = CONFIG_DIR / "run-log.jsonl"
STATUS_CACHE = CONFIG_DIR / "status.json"
SKILL_DIR = BROOMVA_ROOT / "skills" / "bookkeeping"
ENTITY_TEMPLATE = SKILL_DIR / "templates" / "entity-page.md"
RAW_TEMPLATE = SKILL_DIR / "templates" / "raw-extract.md"

PROMOTE_THRESHOLD = 5
DISCARD_THRESHOLD = 2
IMMEDIATE_PROMOTE_THRESHOLD = 7
LLM_JUDGE_AMBIGUOUS_LOW = 3
LLM_JUDGE_AMBIGUOUS_HIGH = 6

ENTITY_TYPES = [
    "concept",
    "pattern",
    "tool",
    "person",
    "project",
    "discovery",
    "question",
]

# Life OS keywords for relevance scoring
LIFE_OS_TERMS = [
    "arcan", "lago", "autonomic", "haima", "anima", "nous", "praxis",
    "vigil", "spaces", "bstack", "egri", "symphony", "autoany",
    "life os", "agent os", "aios", "broomva", "noesis", "opsis",
    "relay", "hive", "haima", "mission-control", "control-metalayer",
    "x402", "spacetimedb", "soul file", "memory", "promotion gate",
    "hysteresis", "bi-temporal", "bitemporal", "event sourcing",
    "knowledge graph", "entity page", "wikilink",
]

# Technical terms that increase novelty when present
TECH_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "not", "this",
    "that", "these", "those", "it", "its", "we", "you", "he", "she",
    "they", "their", "our", "your", "my", "i", "me", "us", "him", "her",
}

# Optional LLM dependency (legacy Gemini judge)
try:
    import google.generativeai as genai  # type: ignore
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

# Optional LLM dependency (authored-agents Anthropic judge — BRO-1015)
#
# Loads the three blessed scorer agents from
# ~/broomva/core/life/agents/bookkeeping-{novelty,specificity,relevance}.md
# and calls them sequentially through the Anthropic API. Each agent
# scores ONE dimension; we aggregate to the existing 0..=9 total. This
# is the architecturally-aligned path: the agent prompts live in
# version-controlled .md files (Layer 3 data per the authored-agents
# spec), not embedded as Python string literals.
try:
    from anthropic import Anthropic  # type: ignore
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import yaml  # type: ignore
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# Resolves to ~/broomva/core/life/agents/. Each blessed bookkeeping
# scorer is loaded from this directory at runtime.
AUTHORED_AGENTS_DIR = BROOMVA_ROOT / "core" / "life" / "agents"


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class RawItem:
    """A normalized knowledge item extracted from a source file."""
    item_id: str
    source_id: str
    source_type: str  # moltbook, x, web, conversation, research
    content: str
    quote: str
    author: str
    timestamp: str
    metadata: dict = field(default_factory=dict)


@dataclass
class LintError:
    """A validation error found in an entity page."""
    file_path: str
    field: str
    message: str
    severity: str = "error"  # error | warning


@dataclass
class ScoredItem:
    """A RawItem with Nous gate scores attached."""
    item: RawItem
    novelty: int       # 0-3
    specificity: int   # 0-3
    relevance: int     # 0-3
    total: int
    promote: bool
    candidate_entities: list[str]
    scoring_method: str  # "heuristic" or "llm_judge"
    reasoning: dict = field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    """Return current UTC timestamp as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:80]


def ensure_dirs() -> None:
    """Create required directories if they don't exist."""
    for d in [ENTITIES_DIR, NOTES_DIR, CONFIG_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    for et in ENTITY_TYPES:
        (ENTITIES_DIR / et).mkdir(parents=True, exist_ok=True)


def existing_entity_slugs() -> list[str]:
    """Return all entity slugs currently in the entities directory."""
    slugs = []
    for et in ENTITY_TYPES:
        type_dir = ENTITIES_DIR / et
        if type_dir.exists():
            for p in type_dir.glob("*.md"):
                slugs.append(p.stem)
    return slugs


def log_run(entry: dict) -> None:
    """Append a run log entry to the JSONL run log."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def update_status_cache(stats: dict) -> None:
    """Write the current stats snapshot to the status cache."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_CACHE.write_text(json.dumps({**stats, "updated_at": now_iso()}, indent=2))


# ── Stage 1: Ingest ───────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Parse YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body_text). If yaml is unavailable,
    returns ({}, full_text).
    """
    if not _YAML_AVAILABLE:
        return {}, text

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    body = text[m.end():]
    return fm, body


def parse_html_frontmatter(text: str) -> tuple[dict, str]:
    """
    Parse YAML frontmatter from an HTML file's leading comment.

    Expects a comment of shape `<!--\\n---\\n…\\n---\\n-->` somewhere in
    the first 4 KB of the document. Returns (frontmatter_dict, body_text);
    body_text is the original text with the matched frontmatter comment
    removed. Returns ({}, original_text) if frontmatter is absent,
    malformed, or not a dict, or if yaml is unavailable.
    """
    if not _YAML_AVAILABLE or not text:
        return {}, text

    head = text[:4096]
    m = re.search(r"<!--\s*\n---\s*\n(.*?)\n---\s*\n-->", head, re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    body = text[: m.start()] + text[m.end():]
    return fm, body


def read_frontmatter(path: Path) -> tuple[dict, str]:
    """
    Read frontmatter from a file, dispatching by extension.

    .md / .markdown → parse_frontmatter
    .html           → parse_html_frontmatter
    other           → ValueError

    Raises FileNotFoundError if the path does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    suffix = path.suffix.lower()
    text = path.read_text(errors="replace")
    if suffix in (".md", ".markdown"):
        return parse_frontmatter(text)
    if suffix == ".html":
        return parse_html_frontmatter(text)
    raise ValueError(f"Unsupported extension for frontmatter: {path.suffix}")


def extract_wikilinks_md(text: str) -> list[tuple[str, str]]:
    """
    Extract wikilinks from Markdown text.

    Returns list of (target_slug, edge_type) tuples. For Markdown,
    edge_type is always "references" (no edge typing in MD wikilink syntax).

    HTML comment blocks are stripped before extraction to avoid false
    positives from commented-out examples.
    """
    body_no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return [
        (link.split("|")[0], "references")
        for link in re.findall(r"\[\[([^\]]+)\]\]", body_no_comments)
    ]


def extract_wikilinks_html(text: str) -> list[tuple[str, str]]:
    """
    Extract typed wikilinks from HTML text.

    Matches `<a … href="…" … data-relation="…">` anchors (attribute order
    is irrelevant) and returns (target_slug, edge_type) tuples — same
    shape as extract_wikilinks_md.

    External hrefs (http://, https://, mailto:, #fragment) and untyped
    anchors (missing data-relation) are skipped. Slug derivation strips
    leading ./ or ../ segments and the .md/.html extension, so
    `../concept/foo.md` becomes `concept/foo`.
    """
    results: list[tuple[str, str]] = []
    for tag in re.findall(r"<a\b([^>]*?)>", text, flags=re.IGNORECASE):
        href_m = re.search(r"""\bhref\s*=\s*(["'])([^"']*)\1""", tag, flags=re.IGNORECASE)
        rel_m = re.search(r"""\bdata-relation\s*=\s*(["'])([^"']*)\1""", tag, flags=re.IGNORECASE)
        if not href_m or not rel_m:
            continue
        href = href_m.group(2)
        if re.match(r"^(?:https?:|mailto:|#)", href, flags=re.IGNORECASE):
            continue
        # Strip leading ./ or ../ segments, then the .md/.html extension.
        slug = re.sub(r"^(?:\.{1,2}/)+", "", href)
        slug = re.sub(r"\.(?:md|html)$", "", slug, flags=re.IGNORECASE)
        results.append((slug, rel_m.group(2)))
    return results


def ingest_file(source_path: Path, verbose: bool = False) -> list[RawItem]:
    """
    Normalize a raw extract file to a list of RawItem objects.

    Supports:
    - Markdown files with YAML frontmatter (## Item blocks)
    - Plain text / log files (one item per non-empty paragraph)
    - JSONL files (each line is a JSON object)
    """
    if not source_path.exists():
        print(f"[ingest] ERROR: {source_path} not found", file=sys.stderr)
        return []

    text = source_path.read_text(errors="replace")
    source_id = source_path.stem
    source_type = _detect_source_type(source_path, text)
    items: list[RawItem] = []

    if source_path.suffix == ".jsonl":
        items = _ingest_jsonl(text, source_id, source_type)
    elif source_path.suffix in (".md", ".markdown"):
        items = _ingest_markdown(text, source_id, source_type)
    else:
        items = _ingest_plaintext(text, source_id, source_type)

    if verbose:
        print(f"[ingest] {source_path.name} → {len(items)} items (type={source_type})")
    return items


def _detect_source_type(path: Path, text: str) -> str:
    """Infer source type from filename or content."""
    name = path.stem.lower()
    if "moltbook" in name or "social" in name:
        return "moltbook"
    if "-x-" in name or name.startswith("x-"):
        return "x"
    if "conversation" in name or "session" in name:
        return "conversation"
    if "research" in name or "notes" in name:
        return "research"
    if "web" in name:
        return "web"
    return "research"


def _make_item(
    source_id: str,
    source_type: str,
    content: str,
    quote: str = "",
    author: str = "",
    timestamp: str = "",
    metadata: dict | None = None,
) -> RawItem:
    return RawItem(
        item_id=str(uuid.uuid4())[:8],
        source_id=source_id,
        source_type=source_type,
        content=content.strip(),
        quote=quote.strip(),
        author=author,
        timestamp=timestamp or now_iso(),
        metadata=metadata or {},
    )


def _ingest_jsonl(text: str, source_id: str, source_type: str) -> list[RawItem]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        # ── Loop-log format ────────────────────────────────────────────────────
        # Each line is a 30-min engagement run with moltbook_comments[], x_posts[],
        # and a notes string. Extract each comment topic and x post as a separate item.
        if "moltbook_comments" in obj or "x_posts" in obj:
            run_id = obj.get("run_id", "")
            ts = obj.get("timestamp", "")
            karma = obj.get("karma", "")

            for cmt in (obj.get("moltbook_comments") or []) if isinstance(obj.get("moltbook_comments"), list) else []:
                topic = cmt.get("topic") or cmt.get("angle") or ""
                if not topic or len(topic) < 20:
                    continue
                post_id = cmt.get("post_id", "")
                angle = cmt.get("angle", "")
                content = f"{topic}\n\nAngle: {angle}" if angle and angle != topic else topic
                items.append(_make_item(
                    source_id=source_id,
                    source_type="moltbook",
                    content=content,
                    quote=topic[:200],
                    author="broomva",
                    timestamp=ts,
                    metadata={"run_id": run_id, "post_id": post_id, "karma": karma},
                ))

            for xp in (obj.get("x_posts") or []) if isinstance(obj.get("x_posts"), list) else []:
                note = xp.get("note", "")
                if not note or len(note) < 20:
                    continue
                items.append(_make_item(
                    source_id=source_id,
                    source_type="x",
                    content=note,
                    quote=note[:200],
                    author="broomva_tech",
                    timestamp=ts,
                    metadata={"run_id": run_id, "tweet_id": xp.get("id", ""), "type": xp.get("type", ""), "karma": karma},
                ))

            # The run-level notes field as a summary item
            notes = obj.get("notes", "")
            if notes and len(notes) > 30:
                items.append(_make_item(
                    source_id=source_id,
                    source_type="moltbook",
                    content=notes,
                    quote=notes[:200],
                    author="broomva",
                    timestamp=ts,
                    metadata={"run_id": run_id, "karma": karma, "item_type": "run-summary"},
                ))
            continue

        # ── Generic JSONL format ───────────────────────────────────────────────
        content = obj.get("content") or obj.get("text") or obj.get("body") or ""
        if not content or len(content) < 20:
            continue
        items.append(_make_item(
            source_id=source_id,
            source_type=source_type,
            content=content,
            quote=obj.get("quote", ""),
            author=obj.get("author", ""),
            timestamp=obj.get("timestamp", ""),
            metadata={k: v for k, v in obj.items() if k not in ("content", "quote", "author", "timestamp")},
        ))
    return items


def _ingest_markdown(text: str, source_id: str, source_type: str) -> list[RawItem]:
    """
    Parse markdown files into RawItems.

    Supports two formats:
    1. social-insights-raw.md format — ## Item N sections with blockquote content,
       **Score** lines, and **Our angle** / **→ Suggested destination** metadata.
    2. synthesis / general notes format — ## section headers as item boundaries,
       with paragraph content below each header.
    3. Fallback — split by paragraph (≥40 chars).
    """
    fm, body = parse_frontmatter(text)
    items = []

    # ── Format 1: ## Item N blocks (social-insights-raw.md) ─────────────────
    # Pattern: ## Item 3 — @author (Platform `post_id`)
    item_pattern = re.compile(r"^## Item \d+", re.MULTILINE)
    item_blocks = item_pattern.split(body)

    if len(item_blocks) > 1:
        for block in item_blocks[1:]:
            lines = block.splitlines()

            # Extract header line (first non-empty after split)
            header = lines[0].strip() if lines else ""
            # Parse author from "— @author (Platform ...)"
            author_match = re.search(r"@(\w[\w\d_]+)", header)
            author = f"@{author_match.group(1)}" if author_match else ""
            post_id_match = re.search(r"`([a-f0-9\-]{6,})`", header)
            post_id = post_id_match.group(1) if post_id_match else ""

            # Extract score from "**Score**: 6/9 — novelty:3 specificity:2 relevance:1"
            score_total = 0
            novelty = specificity = relevance = 0
            for line in lines:
                sm = re.search(r"\*\*Score\*\*[:\s]+(\d+)/9.*?novelty[:\s]*(\d).*?specificity[:\s]*(\d).*?relevance[:\s]*(\d)", line)
                if sm:
                    score_total = int(sm.group(1))
                    novelty, specificity, relevance = int(sm.group(2)), int(sm.group(3)), int(sm.group(4))
                    break

            # Collect blockquote lines as the quote (the external voice)
            quote_lines = []
            in_quote = False
            for line in lines:
                if line.startswith("> "):
                    quote_lines.append(line[2:].strip())
                    in_quote = True
                elif in_quote and line.strip() == ">":
                    quote_lines.append("")  # blank blockquote line
                elif in_quote and not line.startswith(">"):
                    in_quote = False

            quote = "\n".join(quote_lines).strip()

            # Collect "Our angle" content — lines after **Our angle** header
            # (this is the broomva comment text, which is the main content)
            angle_lines = []
            in_angle = False
            for line in lines:
                if re.match(r"\*\*Our angle\*\*", line):
                    in_angle = True
                    # Remainder of this line after the header
                    rest = re.sub(r"\*\*Our angle\*\*[:\s]*", "", line).strip()
                    if rest:
                        angle_lines.append(rest)
                    continue
                if in_angle:
                    if line.startswith("**→") or line.startswith("---"):
                        break
                    if line.startswith("> "):
                        angle_lines.append(line[2:].strip())
                    elif line.strip():
                        angle_lines.append(line.strip())

            angle_text = "\n".join(angle_lines).strip()

            # Main content = our angle (what we said) if present; else the quote
            content = angle_text if len(angle_text) >= 40 else quote
            if not content or len(content) < 20:
                continue

            items.append(_make_item(
                source_id=source_id,
                source_type=source_type,
                content=content,
                quote=quote,
                author=author,
                metadata={
                    **dict(fm),
                    "post_id": post_id,
                    "score_total": score_total,
                    "novelty": novelty,
                    "specificity": specificity,
                    "relevance": relevance,
                    "pre_scored": True,  # already scored by extraction loop
                },
            ))
        return items

    # ── Format 2: ## Section headers as item boundaries (synthesis notes) ───
    section_pattern = re.compile(r"^#{1,3} .+", re.MULTILINE)
    sections = section_pattern.split(body)
    headers = section_pattern.findall(body)

    if len(sections) > 2:  # more than just a preamble
        for header, section_body in zip(headers, sections[1:]):
            section_body = section_body.strip()
            if not section_body or len(section_body) < 60:
                continue
            # Skip table-of-contents-only sections
            if section_body.count("\n") < 2 and not re.search(r"[.!?]", section_body):
                continue
            content = f"{header.lstrip('#').strip()}\n\n{section_body}"
            items.append(_make_item(
                source_id=source_id,
                source_type=source_type,
                content=content.strip(),
                metadata=dict(fm),
            ))
        if items:
            return items

    # ── Format 3: Paragraph fallback ────────────────────────────────────────
    paragraphs = re.split(r"\n{2,}", body)
    for para in paragraphs:
        para = para.strip()
        if not para or para.startswith("#") or para.startswith("---"):
            continue
        if len(para) < 40:
            continue
        items.append(_make_item(
            source_id=source_id,
            source_type=source_type,
            content=para,
            metadata=dict(fm),
        ))
    return items


def _ingest_plaintext(text: str, source_id: str, source_type: str) -> list[RawItem]:
    items = []
    paragraphs = re.split(r"\n{2,}", text)
    for para in paragraphs:
        para = para.strip()
        if len(para) < 40:
            continue
        items.append(_make_item(
            source_id=source_id,
            source_type=source_type,
            content=para,
        ))
    return items


def discover_raw_extracts() -> list[Path]:
    """Find all raw extract files in NOTES_DIR matching the naming convention."""
    if not NOTES_DIR.exists():
        return []
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}-.+-raw\.(md|txt|jsonl)$")
    return sorted(
        p for p in NOTES_DIR.iterdir()
        if p.is_file() and pattern.match(p.name)
    )


# ── Stage 2: Score ────────────────────────────────────────────────────────────

def _count_technical_terms(text: str) -> int:
    """Count unique technical words not in common stop words."""
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_-]{3,}\b", text.lower())
    unique = {w for w in words if w not in TECH_STOP_WORDS}
    return len(unique)


def heuristic_score(item: RawItem) -> tuple[int, int, int]:
    """
    Fast-path Nous gate scoring.

    Returns (novelty, specificity, relevance) each in range [0, 3].
    """
    text = item.content.lower()

    # Novelty: fewer known Life OS hits → more novel
    known_hits = sum(1 for term in LIFE_OS_TERMS if term in text)
    tech_terms = _count_technical_terms(item.content)
    if known_hits >= 4:
        novelty = 0
    elif known_hits >= 1:
        novelty = 1
    elif tech_terms < 5:
        novelty = 2
    else:
        novelty = 3

    # Specificity: length + structural markers
    has_numbers = any(c.isdigit() for c in item.content)
    has_code = "`" in item.content or "```" in item.content
    has_quote = ('"' in item.content or "'" in item.content) and len(item.content) > 100
    has_cause = any(
        w in text
        for w in ["because", "therefore", "means", "in practice", "as a result", "which causes"]
    )
    length_bonus = 1 if len(item.content) > 200 else 0
    extra_length = 1 if len(item.content) > 500 else 0
    specificity = min(3, sum([has_numbers, has_code, has_quote, has_cause]) + length_bonus + extra_length)

    # Relevance: Life OS keyword hits
    relevance = min(3, known_hits)

    return novelty, specificity, relevance


def _build_entity_slug_candidates(item: RawItem) -> list[str]:
    """
    Heuristic extraction of candidate entity slugs from item content.

    Looks for capitalized multi-word phrases and known Life OS module names.
    """
    candidates = []
    text = item.content

    # Known module names as direct candidates
    for term in LIFE_OS_TERMS:
        if term in text.lower() and len(term) > 4:
            candidates.append(slugify(term))

    # Capitalized phrases (2-4 words)
    caps_phrases = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text)
    for phrase in caps_phrases[:5]:
        candidates.append(slugify(phrase))

    # Deduplicate and return up to 5
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result[:5]


def score_item_heuristic(item: RawItem) -> ScoredItem:
    """
    Score a RawItem using the fast-path heuristic only.

    Returns a ScoredItem with scoring_method='heuristic'.
    """
    novelty, specificity, relevance = heuristic_score(item)
    total = novelty + specificity + relevance
    candidates = _build_entity_slug_candidates(item)
    return ScoredItem(
        item=item,
        novelty=novelty,
        specificity=specificity,
        relevance=relevance,
        total=total,
        promote=total >= PROMOTE_THRESHOLD,
        candidate_entities=candidates,
        scoring_method="heuristic",
        reasoning={
            "novelty_basis": "known_term_hits",
            "specificity_basis": "structural_markers",
            "relevance_basis": "life_os_keywords",
        },
    )


def score_item_llm(item: RawItem, existing_slugs: list[str]) -> Optional[ScoredItem]:
    """
    Score a RawItem using the LLM-as-judge (gemini-2.0-flash).

    Returns None if the API call fails or google.generativeai is unavailable.
    Falls back to heuristic score on any error.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not _GENAI_AVAILABLE or not api_key:
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        slug_context = ", ".join(existing_slugs[:40]) if existing_slugs else "none yet"
        system_prompt = (
            "You are a knowledge quality evaluator for a personal AI agent OS knowledge graph. "
            "Score extracted knowledge items on novelty (0-3), specificity (0-3), and relevance (0-3). "
            "novelty: 3=entirely new concept not in the graph, 0=well-known repeated idea. "
            "specificity: 3=concrete, measurable, cites code/numbers/names, 0=vague/generic. "
            "relevance: 3=directly about Life OS modules or agent architecture, 0=unrelated. "
            "Output ONLY valid JSON with keys: novelty, specificity, relevance, total, "
            "candidate_entities (list of entity slugs this item belongs to), reasoning (dict)."
        )
        user_prompt = (
            f"Existing entity slugs (for context): {slug_context}\n\n"
            f"Item source type: {item.source_type}\n"
            f"Item author: {item.author or 'unknown'}\n"
            f"Item content:\n{item.content[:800]}\n\n"
            "Score this item and return JSON only."
        )

        response = model.generate_content(
            f"{system_prompt}\n\n{user_prompt}",
            generation_config={"temperature": 0.1, "max_output_tokens": 512},
        )
        raw = response.text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)
        novelty = int(data.get("novelty", 0))
        specificity = int(data.get("specificity", 0))
        relevance = int(data.get("relevance", 0))
        total = novelty + specificity + relevance
        candidates = [slugify(s) for s in data.get("candidate_entities", [])][:5]

        return ScoredItem(
            item=item,
            novelty=novelty,
            specificity=specificity,
            relevance=relevance,
            total=total,
            promote=total >= PROMOTE_THRESHOLD,
            candidate_entities=candidates,
            scoring_method="llm_judge",
            reasoning=data.get("reasoning", {}),
        )
    except Exception as e:
        return None


# ── Authored-agents scorer (BRO-1015) ─────────────────────────────────────────
#
# Replaces the single-call Gemini judge with three parallel calls to the
# blessed bookkeeping scorer agents at
# ~/broomva/core/life/agents/bookkeeping-{novelty,specificity,relevance}.md.
#
# Why this matters: the agent prompts are version-controlled Layer-3 data
# (per the authored-agents architecture). When a maintainer wants to
# tune the novelty scorer, they edit `bookkeeping-novelty.md` and PR it
# — every Python pipeline run reads the updated prompt. No code change
# required in `bookkeeping.py`.
#
# The authored-agents path is preferred over Gemini when:
# - ANTHROPIC_API_KEY is set AND
# - `anthropic` Python SDK is installed AND
# - The three .md files exist at AUTHORED_AGENTS_DIR
#
# Falls back to Gemini, then heuristic-only, when those aren't met.


def _load_agent_spec(name: str) -> Optional[dict]:
    """
    Load an authored agent spec from disk and return its parsed
    frontmatter + body. Returns None if the file doesn't exist, can't
    be parsed, or `yaml` isn't available (PyYAML is an optional dep).

    The schema mirrors `ergon::parse_agent_md`: the body is the agent's
    `instructions`, the YAML frontmatter holds `model`, `max_turns`,
    `input_schema`, `output_schema`, etc.
    """
    if not _YAML_AVAILABLE:
        return None
    path = AUTHORED_AGENTS_DIR / f"{name}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text()
        # Frontmatter is delimited by `---` at the top of the file.
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        frontmatter = yaml.safe_load(text[4:end])
        body = text[end + 5 :].strip()
        if not isinstance(frontmatter, dict) or not body:
            return None
        return {
            "name": frontmatter.get("name", name),
            "model": frontmatter.get("model", "claude-haiku-4-5"),
            "max_turns": frontmatter.get("max_turns", 1),
            "input_schema": frontmatter.get("input_schema", {}),
            "output_schema": frontmatter.get("output_schema", {}),
            "instructions": body,
        }
    except Exception:
        return None


def _call_authored_scorer(
    spec: dict,
    item: RawItem,
    existing_slugs: list[str],
    client,
) -> Optional[dict]:
    """
    Invoke one authored bookkeeping-* scorer against the Anthropic API.

    Builds the prompt from the agent's `instructions` (loaded verbatim
    from the .md body), passes the item's text as the input matching
    the agent's `input_schema`, asks the model to return JSON matching
    `output_schema`. Validates the structure and returns the parsed
    response dict, or None on any failure (so the caller can fall back
    to the next path).
    """
    # The input shape follows each agent's declared input_schema.
    # All three scorers accept {item_text, source_type, ...}; relevance
    # additionally accepts active_projects / open_questions.
    agent_input = {
        "item_text": item.content[:2000],
        "source_type": item.source_type,
        "source_url": item.source_url or "",
    }
    if spec["name"] == "bookkeeping-novelty":
        agent_input["existing_entity_slugs"] = existing_slugs[:40]
    if spec["name"] == "bookkeeping-relevance":
        # Best-effort population. Concrete active projects / open
        # questions could be threaded through from upstream; this is
        # the minimum that lets the agent score above 0.
        agent_input["active_projects"] = [
            "life-agent-os", "ergon", "lago", "arcan", "haima", "anima", "nous", "praxis",
        ]
        agent_input["open_questions"] = []

    system = spec["instructions"]
    user = (
        "Score the following item. Reply with ONLY a JSON object matching the agent's "
        "output_schema. No prose outside the JSON.\n\n"
        f"Input (matches `{spec['name']}` input_schema):\n"
        f"```json\n{json.dumps(agent_input, indent=2)}\n```\n\n"
        "Required output shape (the agent's `output_schema` — populate every required field):\n"
        f"```json\n{json.dumps(spec['output_schema'], indent=2)}\n```"
    )

    try:
        response = client.messages.create(
            model=spec["model"],
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate text blocks (Anthropic returns a list of content blocks)
        raw = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        # Strip markdown code fences if the model added them anyway.
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        # Minimum required fields per agent's output_schema.
        if not isinstance(data, dict) or "score" not in data:
            return None
        score = data.get("score")
        if not isinstance(score, int) or not (0 <= score <= 3):
            return None
        return data
    except Exception:
        return None


def score_item_authored_agents(
    item: RawItem, existing_slugs: list[str]
) -> Optional[ScoredItem]:
    """
    Score a RawItem using the three blessed bookkeeping scorer agents
    (BRO-1012). Returns None if the path is unavailable (no API key, no
    SDK, missing agent files) so the caller can fall back.
    """
    if not _ANTHROPIC_AVAILABLE:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    specs = {
        dim: _load_agent_spec(f"bookkeeping-{dim}")
        for dim in ("novelty", "specificity", "relevance")
    }
    if not all(specs.values()):
        return None

    try:
        client = Anthropic(api_key=api_key)
    except Exception:
        return None

    results = {}
    reasoning = {}
    for dim, spec in specs.items():
        out = _call_authored_scorer(spec, item, existing_slugs, client)
        if out is None:
            return None  # any-call failure → fall back to next scorer
        results[dim] = out["score"]
        # Each scorer's `reasoning` field is a single string; we
        # aggregate under the dimension key for the ScoredItem.reasoning
        # dict.
        reasoning[f"{dim}_reasoning"] = out.get("reasoning", "")
        # Capture anti-pattern self-reports if the scorer set any.
        warnings = out.get("anti_pattern_warnings") or []
        if warnings:
            reasoning[f"{dim}_anti_patterns"] = warnings

    novelty = results["novelty"]
    specificity = results["specificity"]
    relevance = results["relevance"]
    total = novelty + specificity + relevance

    # Candidate-entity extraction: novelty agent surfaces
    # `closest_existing_slug` when score < 3. Use that as the candidate
    # slug; otherwise build from content.
    candidates = []
    nov_out = results.get("novelty_full") or {}
    # _call_authored_scorer returned only the score, not the full dict.
    # We need to re-thread the closest_existing_slug if available; for
    # now build candidates from content as a fallback.
    candidates = _build_entity_slug_candidates(item)

    return ScoredItem(
        item=item,
        novelty=novelty,
        specificity=specificity,
        relevance=relevance,
        total=total,
        promote=total >= PROMOTE_THRESHOLD,
        candidate_entities=candidates,
        scoring_method="authored_agents",
        reasoning=reasoning,
    )


def score_item(item: RawItem, existing_slugs: list[str], verbose: bool = False) -> ScoredItem:
    """
    Two-pass scorer: heuristic fast-path, then LLM for ambiguous band.

    - Score ≤ DISCARD_THRESHOLD (2): discard immediately, no LLM call.
    - Score ≥ IMMEDIATE_PROMOTE_THRESHOLD (7): promote immediately, no LLM call.
    - Score 3-6: call LLM judge (authored agents preferred, Gemini fallback,
      heuristic-only last resort).
    """
    h = score_item_heuristic(item)

    if h.total <= DISCARD_THRESHOLD or h.total >= IMMEDIATE_PROMOTE_THRESHOLD:
        if verbose:
            print(
                f"  [{item.item_id}] heuristic={h.total}/9 "
                f"(n={h.novelty} s={h.specificity} r={h.relevance}) → fast-path"
            )
        return h

    # Ambiguous band: try the authored-agents path first (BRO-1015 —
    # version-controlled .md prompts at core/life/agents/), fall back to
    # the legacy single-call Gemini judge, then to heuristic-only.
    if verbose:
        print(
            f"  [{item.item_id}] heuristic={h.total}/9 → judge (authored agents first)..."
        )
    authored = score_item_authored_agents(item, existing_slugs)
    if authored is not None:
        if verbose:
            print(
                f"  [{item.item_id}] authored_agents={authored.total}/9 "
                f"(n={authored.novelty} s={authored.specificity} r={authored.relevance})"
            )
        return authored

    llm_result = score_item_llm(item, existing_slugs)
    if llm_result is not None:
        if verbose:
            print(
                f"  [{item.item_id}] llm={llm_result.total}/9 "
                f"(n={llm_result.novelty} s={llm_result.specificity} r={llm_result.relevance})"
            )
        return llm_result

    if verbose:
        print(f"  [{item.item_id}] LLM unavailable, keeping heuristic={h.total}/9")
    return h


# ── Stage 3: Scatter ──────────────────────────────────────────────────────────

def scatter(scored: ScoredItem, verbose: bool = False) -> list[str]:
    """
    Map a single scored item to one or more entity candidate slugs.

    Returns the list of candidate slugs from the scorer, augmented by
    content analysis for items that had no LLM-derived candidates.
    """
    candidates = list(scored.candidate_entities)
    if not candidates:
        candidates = _build_entity_slug_candidates(scored.item)
    if verbose and candidates:
        print(f"  scatter → {candidates}")
    return candidates


# ── Stage 4: Resolve ──────────────────────────────────────────────────────────

def resolve_slug(candidate: str, existing_slugs: list[str]) -> tuple[str, bool]:
    """
    Fuzzy-match a candidate slug against existing entity slugs.

    Returns (resolved_slug, is_existing) where is_existing=True if the
    candidate matches an existing slug (cutoff=0.80), False if it's new.
    """
    matches = difflib.get_close_matches(candidate, existing_slugs, n=1, cutoff=0.80)
    if matches:
        return matches[0], True
    return candidate, False


def resolve_candidates(
    candidates: list[str], existing_slugs: list[str], verbose: bool = False
) -> list[tuple[str, bool]]:
    """
    Resolve all candidate slugs, returning (slug, is_existing) pairs.
    Deduplicated by resolved slug.
    """
    seen = set()
    results = []
    for c in candidates:
        resolved, is_existing = resolve_slug(c, existing_slugs)
        if resolved not in seen:
            seen.add(resolved)
            results.append((resolved, is_existing))
            if verbose:
                tag = "existing" if is_existing else "new"
                print(f"  resolve: {c!r} → {resolved!r} ({tag})")
    return results


# ── Stage 5: Promote ──────────────────────────────────────────────────────────

def _load_entity_template() -> str:
    """
    Return the built-in entity template used by promote_item().

    The external entity-page.md template (ENTITY_TEMPLATE) is the *human-authoring*
    template — its placeholders use descriptive names like {Human-Readable Title} that
    are not intended for programmatic substitution. The built-in default below uses the
    exact keys that content_map in promote_item() populates.
    """
    # Built-in default template — keys match content_map exactly
    return """\
---
slug: {slug}
type: {entity_type}
status: candidate
core_claim: "{core_claim}"
sources:
  - {source_ref}
related: []
created: {created}
updated: {updated}
tags:
  - {entity_type}
  - bookkeeping
---

# {title}

## Core Claim

{core_claim}

## Evidence

> {quote}

Source: {source_ref} | Score: {score}/9 (n={novelty} s={specificity} r={relevance})

## Context

{content}

## Related

<!-- Add wikilinks to related entities here, e.g. [[arcan]] [[memory]] -->

## Open Questions

<!-- What remains unclear? -->

## Synthesis Notes

<!-- Populated by synthesize stage -->
"""


def _infer_entity_type(slug: str, item: RawItem) -> str:
    """Guess entity type from slug and content."""
    text = (slug + " " + item.content).lower()
    if any(w in text for w in ["pattern", "approach", "method", "strategy", "technique"]):
        return "pattern"
    if any(w in text for w in ["tool", "library", "framework", "sdk", "cli", "api"]):
        return "tool"
    if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", item.author or ""):
        return "person"
    if any(w in text for w in ["project", "platform", "product", "app", "system"]):
        return "project"
    if "?" in item.content or any(w in text for w in ["why", "how", "what is", "open question"]):
        return "question"
    if any(w in text for w in ["discovered", "found", "insight", "breakthrough"]):
        return "discovery"
    return "concept"


def promote_item(
    scored: ScoredItem,
    entity_slug: str,
    entity_type: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Path | None:
    """
    Write an entity page for a scored item.

    Creates research/entities/{entity_type}/{entity_slug}.md using the template.
    Returns the path written, or None in dry_run mode or on error.
    """
    if entity_type is None:
        entity_type = _infer_entity_type(entity_slug, scored.item)

    entity_dir = ENTITIES_DIR / entity_type
    entity_path = entity_dir / f"{entity_slug}.md"

    if entity_path.exists():
        # Update 'updated' field in existing page rather than overwriting
        if not dry_run:
            _update_entity_timestamp(entity_path)
        if verbose:
            print(f"  [promote] updated existing: {entity_path.relative_to(BROOMVA_ROOT)}")
        return entity_path

    template = _load_entity_template()
    title = entity_slug.replace("-", " ").title()
    # core_claim must be a single YAML-safe line — strip newlines and escape double quotes
    raw_claim = scored.item.content.replace("\n", " ").replace("\r", " ").replace('"', "'")
    raw_claim = re.sub(r"\s+", " ", raw_claim).strip()
    core_claim = (raw_claim[:137] + "...") if len(raw_claim) > 140 else raw_claim
    source_ref = scored.item.source_id
    today = today_str()

    # Substitute all {placeholder} patterns
    content_map = {
        "slug": entity_slug,
        "entity_type": entity_type,
        "title": title,
        "core_claim": core_claim,
        "source_ref": source_ref,
        "created": today,
        "updated": today,
        "content": scored.item.content,
        "quote": scored.item.quote or scored.item.content[:200],
        "score": str(scored.total),
        "novelty": str(scored.novelty),
        "specificity": str(scored.specificity),
        "relevance": str(scored.relevance),
    }
    page = template
    for key, value in content_map.items():
        page = page.replace("{" + key + "}", value)

    if not dry_run:
        entity_dir.mkdir(parents=True, exist_ok=True)
        entity_path.write_text(page)
        if verbose:
            print(f"  [promote] created: {entity_path.relative_to(BROOMVA_ROOT)}")
    else:
        if verbose:
            print(f"  [promote] dry-run: would create {entity_path.relative_to(BROOMVA_ROOT)}")

    return entity_path if not dry_run else None


def _update_entity_timestamp(entity_path: Path) -> None:
    """Update the 'updated' field in an existing entity page frontmatter."""
    text = entity_path.read_text()
    today = today_str()
    updated = re.sub(r"(^updated:\s*)(.+)$", rf"\g<1>{today}", text, flags=re.MULTILINE)
    entity_path.write_text(updated)


# ── Stage 6: Synthesize ───────────────────────────────────────────────────────

def find_synthesis_candidates(verbose: bool = False) -> list[dict]:
    """
    Detect entity clusters that may warrant a synthesis note.

    A cluster is a group of ≥2 entities that share a common keyword in their
    core_claim or content. Returns list of cluster descriptors.
    """
    if not ENTITIES_DIR.exists():
        return []

    entity_files = list(ENTITIES_DIR.rglob("*.md"))
    if verbose:
        print(f"[synthesize] Scanning {len(entity_files)} entity pages...")

    # Build keyword → [slugs] map
    keyword_map: dict[str, list[str]] = {}
    for ef in entity_files:
        slug = ef.stem
        text = ef.read_text(errors="replace").lower()
        for term in LIFE_OS_TERMS + ["event sourcing", "trust", "policy", "governance"]:
            if term in text:
                keyword_map.setdefault(term, []).append(slug)

    candidates = []
    for term, slugs in sorted(keyword_map.items(), key=lambda x: -len(x[1])):
        if len(slugs) >= 2:
            candidates.append({
                "topic": term,
                "entity_count": len(slugs),
                "slugs": slugs[:10],
            })

    # Deduplicate by overlapping slug sets (keep largest clusters)
    seen_slugs: set[str] = set()
    filtered = []
    for c in candidates:
        slug_set = set(c["slugs"])
        if not slug_set.issubset(seen_slugs):
            filtered.append(c)
            seen_slugs.update(slug_set)

    return filtered[:20]


# ── Stage 7: Lint ─────────────────────────────────────────────────────────────

def lint_entity_page(entity_path: Path) -> list[LintError]:
    """
    Validate a single entity page.

    Checks:
    - YAML frontmatter parseable
    - core_claim exists and is ≤140 characters
    - sources is a non-empty list
    - related entries match [[wikilink]] format
    - referenced wikilinks resolve to existing entity slugs
    """
    errors: list[LintError] = []
    path_str = str(entity_path)

    if not entity_path.exists():
        errors.append(LintError(path_str, "file", "File does not exist", "error"))
        return errors

    text = entity_path.read_text(errors="replace")
    fm, body = parse_frontmatter(text)

    if not fm:
        if not _YAML_AVAILABLE:
            errors.append(LintError(path_str, "yaml", "PyYAML not installed, skipping frontmatter lint", "warning"))
        else:
            errors.append(LintError(path_str, "frontmatter", "Missing or unparseable YAML frontmatter", "error"))
        return errors

    # core_claim
    core_claim = fm.get("core_claim", "")
    if not core_claim:
        errors.append(LintError(path_str, "core_claim", "core_claim is missing", "error"))
    elif len(str(core_claim)) > 140:
        errors.append(LintError(
            path_str, "core_claim",
            f"core_claim is {len(str(core_claim))} chars (max 140)", "error"
        ))

    # sources
    sources = fm.get("sources", [])
    if not sources or not isinstance(sources, list):
        errors.append(LintError(path_str, "sources", "sources must be a non-empty list", "error"))

    # status
    valid_statuses = {"candidate", "entity", "synthesis", "raw", "archived"}
    status = fm.get("status", "")
    if status and status not in valid_statuses:
        errors.append(LintError(
            path_str, "status",
            f"status {status!r} not in {valid_statuses}", "warning"
        ))

    # type
    entity_type = fm.get("type", "")
    if entity_type and entity_type not in ENTITY_TYPES:
        errors.append(LintError(
            path_str, "type",
            f"type {entity_type!r} not in {ENTITY_TYPES}", "warning"
        ))

    # related: must be wikilink format
    related = fm.get("related", [])
    if isinstance(related, list):
        for ref in related:
            if ref and not re.match(r"^\[\[.+\]\]$", str(ref)):
                errors.append(LintError(
                    path_str, "related",
                    f"related entry {ref!r} is not [[wikilink]] format", "error"
                ))

    # Resolve wikilinks in body — skip HTML comment lines to avoid false positives
    wikilinks = extract_wikilinks_md(body)
    existing = set(existing_entity_slugs())
    for target, _edge in wikilinks:
        slug = slugify(target)
        if slug and slug not in existing:
            errors.append(LintError(
                path_str, "wikilink",
                f"Broken wikilink: [[{target}]] (slug {slug!r} not found)", "warning"
            ))

    return errors


def lint_all(verbose: bool = False) -> list[LintError]:
    """Run lint_entity_page on all entity pages and aggregate errors."""
    all_errors: list[LintError] = []
    if not ENTITIES_DIR.exists():
        return all_errors
    pages = list(ENTITIES_DIR.rglob("*.md"))
    if verbose:
        print(f"[lint] Checking {len(pages)} entity pages...")
    for page in pages:
        errs = lint_entity_page(page)
        all_errors.extend(errs)
        if verbose and errs:
            for e in errs:
                print(f"  [{e.severity.upper()}] {Path(e.file_path).name}: {e.field} — {e.message}")
    return all_errors


def lint_format_discernment(root: Path) -> list[LintError]:
    """
    Format-discernment lint checks (P17, see SKILL.md "Format Discernment").

    Currently implements:
      - stale_projection: <note>.md mtime > <note>.html mtime → warn

    Three additional checks land in subsequent commits:
      - broken_canonical, substrate_violation, unregistered_c
    """
    errors: list[LintError] = []
    if not root.exists():
        return errors
    for html_path in root.rglob("*.html"):
        md_path = html_path.with_suffix(".md")
        if md_path.exists() and md_path.stat().st_mtime > html_path.stat().st_mtime:
            errors.append(LintError(
                str(html_path),
                "stale_projection",
                f"{md_path.name} is newer than {html_path.name} — "
                f"rerun `bookkeeping render` to refresh",
                "warning",
            ))
    return errors


# ── Full Pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    source_files: list[Path] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Execute the full 7-stage bookkeeping pipeline.

    Returns a run log entry dict with pipeline statistics.
    """
    start_time = time.time()
    run_id = int(time.time())

    ensure_dirs()

    # ── Auto-discover sources if none given ──
    if not source_files:
        source_files = discover_raw_extracts()
        if verbose:
            print(f"[run] Auto-discovered {len(source_files)} raw extract files")

    if not source_files:
        print("[run] No source files found. Use --source or add raw extracts to research/notes/")
        return {}

    existing_slugs = existing_entity_slugs()

    # Stage counters
    items_ingested = 0
    items_scored = 0
    items_promoted = 0
    items_discarded = 0
    items_raw_only = 0
    entities_created = 0
    entities_updated = 0
    scoring_breakdown = {"heuristic": 0, "llm_judge": 0}

    all_scored: list[ScoredItem] = []

    # ── Stage 1+2+3+4: Ingest → Score → Scatter → Resolve ──
    for src in source_files:
        if verbose:
            print(f"\n[run] Processing: {src.name}")

        raw_items = ingest_file(src, verbose=verbose)
        items_ingested += len(raw_items)

        for item in raw_items:
            scored = score_item(item, existing_slugs, verbose=verbose)
            scoring_breakdown[scored.scoring_method] = (
                scoring_breakdown.get(scored.scoring_method, 0) + 1
            )
            items_scored += 1

            if scored.total <= DISCARD_THRESHOLD:
                items_discarded += 1
                if verbose:
                    print(f"  [{item.item_id}] DISCARD score={scored.total}/9")
                continue

            candidates = scatter(scored, verbose=verbose)
            resolved = resolve_candidates(candidates, existing_slugs, verbose=verbose)

            if not resolved:
                items_raw_only += 1
                if verbose:
                    print(f"  [{item.item_id}] no candidates → raw-only")
                continue

            all_scored.append(scored)

    # ── Stage 5: Promote ──
    print(f"\n[run] Promoting {len(all_scored)} items (threshold ≥{PROMOTE_THRESHOLD})...")
    for scored in all_scored:
        if scored.total < PROMOTE_THRESHOLD:
            items_raw_only += 1
            continue

        candidates = scatter(scored)
        resolved = resolve_candidates(candidates, existing_slugs)
        if not resolved:
            items_raw_only += 1
            continue

        for slug, is_existing in resolved[:2]:  # max 2 entities per item
            path = promote_item(scored, slug, dry_run=dry_run, verbose=verbose)
            if path is not None or dry_run:
                if is_existing:
                    entities_updated += 1
                else:
                    entities_created += 1
                    existing_slugs.append(slug)

        items_promoted += 1

    # ── Stage 6: Synthesize ──
    synthesis_candidates = find_synthesis_candidates(verbose=verbose)
    if synthesis_candidates and verbose:
        print(f"\n[run] Synthesis candidates: {len(synthesis_candidates)}")
        for c in synthesis_candidates[:5]:
            print(f"  topic={c['topic']!r} entities={c['entity_count']}")

    # ── Stage 7: Lint ──
    lint_errors = lint_all(verbose=verbose)
    lint_error_count = len([e for e in lint_errors if e.severity == "error"])

    duration = round(time.time() - start_time, 2)

    entry = {
        "run_id": run_id,
        "timestamp": now_iso(),
        "source_files": [str(s) for s in source_files],
        "items_ingested": items_ingested,
        "items_scored": items_scored,
        "items_promoted": items_promoted,
        "items_discarded": items_discarded,
        "items_raw_only": items_raw_only,
        "entities_created": entities_created,
        "entities_updated": entities_updated,
        "synthesis_candidates": len(synthesis_candidates),
        "lint_errors": lint_error_count,
        "scoring_breakdown": scoring_breakdown,
        "duration_seconds": duration,
    }

    if not dry_run:
        log_run(entry)
        # Update status cache
        _refresh_status_cache()

    print(f"\n[run] Done in {duration}s")
    print(f"  Ingested: {items_ingested} | Scored: {items_scored} | Promoted: {items_promoted}")
    print(f"  Discarded: {items_discarded} | Raw-only: {items_raw_only}")
    print(f"  Entities created: {entities_created} | Updated: {entities_updated}")
    print(f"  Synthesis candidates: {len(synthesis_candidates)} | Lint errors: {lint_error_count}")
    if dry_run:
        print("  [DRY RUN] No files written.")

    return entry


# ── Status ────────────────────────────────────────────────────────────────────

def _refresh_status_cache() -> dict:
    """Recompute entity graph stats and write to status cache."""
    stats: dict = {
        "total_entities": 0,
        "by_type": {},
        "by_status": {},
        "recent_promotions_7d": 0,
        "lint_errors": 0,
        "last_run": None,
    }

    if ENTITIES_DIR.exists():
        for et in ENTITY_TYPES:
            type_dir = ENTITIES_DIR / et
            if not type_dir.exists():
                continue
            pages = list(type_dir.glob("*.md"))
            stats["by_type"][et] = len(pages)
            stats["total_entities"] += len(pages)

            cutoff = datetime.now() - timedelta(days=7)
            for p in pages:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime >= cutoff:
                    stats["recent_promotions_7d"] += 1

                # Count by status
                text = p.read_text(errors="replace")
                fm, _ = parse_frontmatter(text)
                status = fm.get("status", "unknown") if fm else "unknown"
                stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

    if RUN_LOG.exists():
        lines = RUN_LOG.read_text().strip().splitlines()
        if lines:
            try:
                last = json.loads(lines[-1])
                stats["last_run"] = last.get("timestamp")
                stats["lint_errors"] = last.get("lint_errors", 0)
            except Exception:
                pass

    update_status_cache(stats)
    return stats


def run_status() -> None:
    """Print a formatted knowledge graph status report."""
    # Try cached stats first
    stats: dict = {}
    if STATUS_CACHE.exists():
        try:
            stats = json.loads(STATUS_CACHE.read_text())
        except Exception:
            pass

    if not stats:
        stats = _refresh_status_cache()

    total = stats.get("total_entities", 0)
    by_type = stats.get("by_type", {})
    by_status = stats.get("by_status", {})
    recent = stats.get("recent_promotions_7d", 0)
    lint_errors = stats.get("lint_errors", 0)
    last_run = stats.get("last_run", "never")
    updated_at = stats.get("updated_at", "?")

    print("\nKnowledge Graph Status")
    print("=" * 40)
    print(f"Total entities: {total}")

    type_parts = " | ".join(f"{t}: {by_type.get(t, 0)}" for t in ENTITY_TYPES if by_type.get(t, 0) > 0)
    if type_parts:
        print(f"  {type_parts}")

    status_parts = " | ".join(f"{s}: {c}" for s, c in sorted(by_status.items()))
    if status_parts:
        print(f"Status breakdown: {status_parts}")

    print(f"Recent promotions (last 7 days): {recent}")
    print(f"Lint errors: {lint_errors}")
    print(f"Last run: {last_run}")
    print(f"Cache updated: {updated_at}")

    # Show recent run log entries
    if RUN_LOG.exists():
        lines = RUN_LOG.read_text().strip().splitlines()
        if lines:
            print(f"\nRecent runs ({min(3, len(lines))} of {len(lines)}):")
            for line in lines[-3:]:
                try:
                    r = json.loads(line)
                    ts = r.get("timestamp", "?")[:19]
                    print(
                        f"  {ts} | "
                        f"ingested={r.get('items_ingested',0)} "
                        f"promoted={r.get('items_promoted',0)} "
                        f"created={r.get('entities_created',0)} "
                        f"({r.get('duration_seconds',0)}s)"
                    )
                except Exception:
                    pass


# ── Query ─────────────────────────────────────────────────────────────────────

def run_query(slug: str, verbose: bool = False) -> None:
    """Find and display an entity page by slug (fuzzy matched)."""
    if not ENTITIES_DIR.exists():
        print(f"[query] No entities directory at {ENTITIES_DIR}")
        return

    all_pages: dict[str, Path] = {}
    for et in ENTITY_TYPES:
        type_dir = ENTITIES_DIR / et
        if type_dir.exists():
            for p in type_dir.glob("*.md"):
                all_pages[p.stem] = p

    if not all_pages:
        print("[query] No entity pages found.")
        return

    # Exact match first
    if slug in all_pages:
        path = all_pages[slug]
    else:
        # Fuzzy match
        matches = difflib.get_close_matches(slug, list(all_pages.keys()), n=3, cutoff=0.5)
        if not matches:
            print(f"[query] No entity found for {slug!r}")
            print(f"  Available ({len(all_pages)}): {', '.join(list(all_pages.keys())[:10])}...")
            return
        if len(matches) == 1 or matches[0] == slug:
            path = all_pages[matches[0]]
        else:
            print(f"[query] Multiple matches for {slug!r}:")
            for m in matches:
                print(f"  {m} ({all_pages[m].relative_to(BROOMVA_ROOT)})")
            path = all_pages[matches[0]]
            print(f"  → Showing {matches[0]}")

    print(f"\n{path.relative_to(BROOMVA_ROOT)}")
    print("─" * 60)
    print(path.read_text())


# ── CLI Subcommands ───────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    """Execute the full 7-stage pipeline."""
    sources: list[Path] | None = None
    if args.source:
        sources = [Path(args.source)]
        if not sources[0].exists():
            print(f"ERROR: {args.source} not found", file=sys.stderr)
            sys.exit(1)

    run_pipeline(
        source_files=sources,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    """Normalize a single file and print JSON to stdout."""
    path = Path(args.source)
    items = ingest_file(path, verbose=args.verbose)
    print(json.dumps([asdict(i) for i in items], indent=2))


def cmd_score(args: argparse.Namespace) -> None:
    """Score all items in a raw extract file and print results."""
    path = Path(args.file)
    items = ingest_file(path, verbose=args.verbose)
    existing = existing_entity_slugs()
    results = []
    for item in items:
        scored = score_item(item, existing, verbose=args.verbose)
        results.append({
            "item_id": item.item_id,
            "content_preview": item.content[:80],
            "novelty": scored.novelty,
            "specificity": scored.specificity,
            "relevance": scored.relevance,
            "total": scored.total,
            "promote": scored.promote,
            "method": scored.scoring_method,
            "candidates": scored.candidate_entities,
        })

    for r in results:
        promote_str = "PROMOTE" if r["promote"] else "discard"
        print(
            f"[{r['item_id']}] {r['total']}/9 "
            f"(n={r['novelty']} s={r['specificity']} r={r['relevance']}) "
            f"[{r['method']}] → {promote_str}"
        )
        print(f"  {r['content_preview']!r}")
        if r["candidates"]:
            print(f"  candidates: {r['candidates']}")


def cmd_promote(args: argparse.Namespace) -> None:
    """Promote pending items (score ≥ threshold) from a raw extract to entity pages."""
    path = Path(args.file)
    items = ingest_file(path, verbose=args.verbose)
    existing = existing_entity_slugs()
    ensure_dirs()

    promoted = 0
    for item in items:
        scored = score_item(item, existing, verbose=args.verbose)
        if scored.total < PROMOTE_THRESHOLD:
            if args.verbose:
                print(f"  SKIP [{item.item_id}] score={scored.total}/9 < {PROMOTE_THRESHOLD}")
            continue

        candidates = scatter(scored, verbose=args.verbose)
        resolved = resolve_candidates(candidates, existing, verbose=args.verbose)
        if not resolved:
            print(f"  [{item.item_id}] no entity candidates, skipping")
            continue

        for slug, is_existing in resolved[:1]:
            promote_item(scored, slug, dry_run=args.dry_run, verbose=True)
            if not is_existing:
                existing.append(slug)
        promoted += 1

    print(f"\n[promote] Done: {promoted} items promoted from {path.name}")
    if args.dry_run:
        print("[promote] DRY RUN — no files written")


def cmd_replay(args: argparse.Namespace) -> None:
    """Replay scoring/promotion against a frozen snapshot of `research/entities/`.

    Closes the corruption mode where bookkeeping reads from the same graph it
    writes to (the "shadow dream" failure described in the multi-tier-dreaming
    research entity). Replay phase = run the full pipeline against a frozen
    copy, generate a diff, surface it for review. Promotion to the live graph
    requires explicit `--commit`.

    Five-phase shape (per multi-tier-dreaming entity):
      1. Gather   — read the source file as the dense lower-tier signal
      2. Replay   — score+promote against a FROZEN copy of research/entities/
      3. Prune    — items below threshold or failing lint are flagged
      4. Consolidate — `--commit` mode applies the diff to the live graph
      5. Index    — git diff output is the audit trail of what changed

    Without --commit, replay is a pure read operation: it copies entities to
    a tempdir, runs scoring against that copy, and prints the proposed diff.
    With --commit, replay re-runs the same logic against the LIVE graph (so
    the diff applies to the same starting state the human approved).
    """
    import shutil
    import tempfile

    source_path = Path(args.source) if args.source else None
    if source_path and not source_path.exists():
        print(f"ERROR: {source_path} not found", file=sys.stderr)
        sys.exit(1)

    live_entities = ENTITIES_DIR
    if not live_entities.exists():
        print(f"ERROR: {live_entities} not found", file=sys.stderr)
        sys.exit(1)

    # Phase 1: Gather (which sources are we replaying?)
    if source_path:
        sources = [source_path]
    else:
        sources = list(NOTES_DIR.glob("*-raw.md"))
        sources = [p for p in sources if p.is_file()]
        if not sources:
            print("[replay] no raw extract files in research/notes/", file=sys.stderr)
            sys.exit(0)

    print(f"[replay] Phase 1 (Gather): {len(sources)} source file(s)")
    for s in sources[:5]:
        try:
            print(f"  - {s.relative_to(BROOMVA_ROOT)}")
        except ValueError:
            print(f"  - {s}")
    if len(sources) > 5:
        print(f"  ... +{len(sources) - 5} more")

    # Phase 2: Replay against frozen substrate
    with tempfile.TemporaryDirectory(prefix="bookkeeping-replay-") as tmpdir:
        frozen_root = Path(tmpdir)
        frozen_entities = frozen_root / "research" / "entities"
        frozen_entities.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(live_entities, frozen_entities)
        print(f"\n[replay] Phase 2 (Replay): froze {sum(1 for _ in frozen_entities.rglob('*.md'))} entity files at {frozen_entities}")

        # Track what would change in the replay world (but don't actually
        # write to it — we use existing_entity_slugs() against the frozen
        # state for collision detection, and report counts only).
        existing_frozen = sorted(p.stem for p in frozen_entities.rglob("*.md"))

        promoted = 0
        skipped = 0
        scores = []
        for src in sources:
            try:
                items = ingest_file(src, verbose=args.verbose)
            except Exception as e:
                print(f"  ! ingest failed for {src.name}: {e}", file=sys.stderr)
                continue
            for item in items:
                try:
                    scored = score_item(item, existing_frozen, verbose=False)
                except Exception as e:
                    print(f"  ! score failed for {item.item_id}: {e}", file=sys.stderr)
                    continue
                scores.append(scored.total)
                if scored.total < PROMOTE_THRESHOLD:
                    skipped += 1
                    continue
                # Would-promote: simulate without writing
                promoted += 1
                if args.verbose:
                    print(f"  WOULD-PROMOTE [{item.item_id}] score={scored.total}/9 → {scored.suggested_slug}")

        print(f"\n[replay] Phase 3 (Prune): {skipped} item(s) below threshold; {promoted} would-promote")
        if scores:
            print(f"          score distribution: min={min(scores)} max={max(scores)} mean={sum(scores)/len(scores):.1f}")

    # Phase 4: Consolidate (--commit only)
    if args.commit:
        print(f"\n[replay] Phase 4 (Consolidate): --commit set; running pipeline against LIVE graph")
        run_pipeline(
            source_files=sources,
            dry_run=False,
            verbose=args.verbose,
        )
        # Phase 5: Index (the agent / user inspects git diff to verify)
        print(f"\n[replay] Phase 5 (Index): inspect `git diff research/entities/` for the audit trail")
    else:
        print(f"\n[replay] Phase 4 (Consolidate): SKIPPED — pass --commit to apply the diff to the live graph")
        print(f"          to inspect what would change without committing, run:")
        print(f"            bookkeeping run --dry-run --source <file>")


def cmd_synthesize(args: argparse.Namespace) -> None:
    """Detect entity clusters and flag synthesis candidates."""
    candidates = find_synthesis_candidates(verbose=args.verbose)
    if not candidates:
        print("[synthesize] No synthesis candidates found.")
        return

    print(f"\n[synthesize] {len(candidates)} synthesis candidates:")
    for c in candidates:
        print(f"\n  Topic: {c['topic']!r} ({c['entity_count']} entities)")
        for slug in c["slugs"][:5]:
            print(f"    - {slug}")
        if len(c["slugs"]) > 5:
            print(f"    ... and {len(c['slugs']) - 5} more")


def cmd_lint(args: argparse.Namespace) -> None:
    """Validate entity pages for frontmatter correctness and broken wikilinks."""
    if args.all or not args.file:
        errors = lint_all(verbose=args.verbose)
    else:
        path = Path(args.file)
        errors = lint_entity_page(path)

    if not errors:
        print("[lint] No errors found.")
        return

    error_count = len([e for e in errors if e.severity == "error"])
    warning_count = len([e for e in errors if e.severity == "warning"])

    for e in errors:
        label = "ERROR" if e.severity == "error" else "WARN "
        file_name = Path(e.file_path).name
        print(f"[{label}] {file_name}: {e.field} — {e.message}")

    print(f"\n[lint] {len(errors)} issues: {error_count} errors, {warning_count} warnings")
    if error_count > 0:
        sys.exit(1)


def cmd_status(_args: argparse.Namespace) -> None:
    """Print knowledge graph statistics."""
    run_status()


def cmd_query(args: argparse.Namespace) -> None:
    """Find and display an entity page."""
    run_query(args.slug, verbose=getattr(args, "verbose", False))


def cmd_render(args: argparse.Namespace) -> None:
    """
    Category B projection: render a Layer 4 synthesis MD into a single-file HTML.

    Path resolution:
      - file `.md`  → render to sibling `.html`
      - directory   → render all `*-synthesis.md` inside (non-recursive by default)
      - --layer N   → render all Layer-N synthesis notes under research/notes/
    """
    targets: list[Path] = []
    src = Path(args.path) if args.path else None

    if args.layer is not None:
        from_notes = Path("research/notes")
        if not from_notes.exists():
            print(f"[render] research/notes/ not found in {Path.cwd()}", file=sys.stderr)
            sys.exit(2)
        if args.layer == 4:
            targets = sorted(from_notes.glob("*-synthesis.md"))
        else:
            print(f"[render] --layer {args.layer}: only layer 4 supported today",
                  file=sys.stderr)
            sys.exit(2)
    elif src is None:
        print("[render] usage: bookkeeping render <path> | --layer N", file=sys.stderr)
        sys.exit(2)
    elif src.is_dir():
        targets = sorted(src.glob("*-synthesis.md"))
    elif src.is_file():
        targets = [src]
    else:
        print(f"[render] not found: {src}", file=sys.stderr)
        sys.exit(2)

    if not targets:
        print("[render] no synthesis notes matched")
        return

    rendered = 0
    for md_path in targets:
        try:
            md_text = md_path.read_text(errors="replace")
            html = render_markdown_to_html(md_text, md_path, link_html=args.link_html)
            out_path = md_path.with_suffix(".html")
            out_path.write_text(html)
            rendered += 1
            if args.verbose:
                print(f"[render] {md_path} → {out_path}")
        except Exception as exc:
            print(f"[render] failed {md_path}: {exc}", file=sys.stderr)
    print(f"[render] {rendered} file(s) rendered")


# ── Entry Point ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="bookkeeping",
        description="Broomva knowledge engine (bstack P8) — 7-stage pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # run
    p_run = sub.add_parser("run", help="Full 7-stage pipeline")
    p_run.add_argument("--source", metavar="FILE", help="Source file (auto-discovers if omitted)")
    p_run.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    p_run.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_run.set_defaults(func=cmd_run)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Normalize a file to JSON")
    p_ingest.add_argument("--source", required=True, metavar="FILE", help="Source file to ingest")
    p_ingest.add_argument("--verbose", "-v", action="store_true")
    p_ingest.set_defaults(func=cmd_ingest)

    # score
    p_score = sub.add_parser("score", help="Score all items in a raw extract")
    p_score.add_argument("--file", required=True, metavar="FILE", help="Raw extract file")
    p_score.add_argument("--verbose", "-v", action="store_true")
    p_score.set_defaults(func=cmd_score)

    # promote
    p_promote = sub.add_parser("promote", help="Promote items (score ≥5) to entity pages")
    p_promote.add_argument("--file", required=True, metavar="FILE", help="Raw extract file")
    p_promote.add_argument("--dry-run", action="store_true")
    p_promote.add_argument("--verbose", "-v", action="store_true")
    p_promote.set_defaults(func=cmd_promote)

    # replay (P6 extension — closes the shadow-dream corruption mode)
    p_replay = sub.add_parser(
        "replay",
        help="Replay scoring/promotion against a FROZEN snapshot of research/entities/ "
             "(closes the corruption mode where bookkeeping reads from the graph it writes to)",
    )
    p_replay.add_argument("--source", metavar="FILE",
                          help="Source raw extract (auto-discovers all *-raw.md if omitted)")
    p_replay.add_argument("--commit", action="store_true",
                          help="Apply the proposed promotions to the live graph "
                               "(default: dry-run only — print would-promote counts)")
    p_replay.add_argument("--verbose", "-v", action="store_true")
    p_replay.set_defaults(func=cmd_replay)

    # synthesize
    p_synth = sub.add_parser("synthesize", help="Detect entity clusters for synthesis")
    p_synth.add_argument("--verbose", "-v", action="store_true")
    p_synth.set_defaults(func=cmd_synthesize)

    # lint
    p_lint = sub.add_parser("lint", help="Validate entity pages")
    p_lint.add_argument("--all", action="store_true", help="Lint all entity pages")
    p_lint.add_argument("--file", metavar="FILE", help="Lint a specific entity page")
    p_lint.add_argument("--verbose", "-v", action="store_true")
    p_lint.set_defaults(func=cmd_lint)

    # status
    p_status = sub.add_parser("status", help="Print knowledge graph stats")
    p_status.set_defaults(func=cmd_status)

    # query
    p_query = sub.add_parser("query", help="Find and display an entity page")
    p_query.add_argument("slug", help="Entity slug (fuzzy matched)")
    p_query.add_argument("--verbose", "-v", action="store_true")
    p_query.set_defaults(func=cmd_query)

    # render (Category B projection — MD canonical → single-file HTML)
    p_render = sub.add_parser(
        "render",
        help="Project a Layer 4 synthesis MD to a single-file HTML (Category B)",
    )
    p_render.add_argument("path", nargs="?", help="MD file or directory of -synthesis.md notes")
    p_render.add_argument("--layer", type=int, default=None,
                          help="Render all notes at the given layer (currently only 4)")
    p_render.add_argument("--link-html", action="store_true",
                          help="Rewrite [[slug]] to .html targets instead of .md")
    p_render.add_argument("--verbose", action="store_true", help="Print each rendered file")
    p_render.set_defaults(func=cmd_render)

    return parser


def main() -> None:
    """Main entry point for the bookkeeping CLI."""
    # Dependency warnings (non-fatal)
    if not _GENAI_AVAILABLE:
        print(
            "[bookkeeping] Note: google-generativeai not installed. "
            "LLM judge disabled (heuristic-only scoring).",
            file=sys.stderr,
        )
    if not _YAML_AVAILABLE:
        print(
            "[bookkeeping] Note: PyYAML not installed. "
            "Frontmatter parsing and lint checks degraded.",
            file=sys.stderr,
        )

    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
