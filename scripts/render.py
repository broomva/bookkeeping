"""
bookkeeping render — Category B projection (MD canonical → single-file HTML).

The HTML output is deterministic: same input markdown produces byte-identical
HTML. Frontmatter is preserved verbatim as a leading HTML comment, with a
`canonical:` field injected to point back to the source MD. Wikilinks are
rewritten to typed <a> tags so the HTML can re-join the knowledge graph.

No external dependencies beyond mistune and PyYAML.
"""
from __future__ import annotations

import re
from pathlib import Path
from string import Template

import mistune
import yaml

# Resolve template directory relative to this file (skill is portable, not site-installed)
SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPTS_DIR.parent / "templates"
TEMPLATE_HTML = TEMPLATES_DIR / "render-template.html"
TEMPLATE_CSS = TEMPLATES_DIR / "render-style.css"

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _load_template() -> Template:
    """Load the HTML template (string.Template with ${name} placeholders)."""
    return Template(TEMPLATE_HTML.read_text())


def _load_css() -> str:
    return TEMPLATE_CSS.read_text()


def _split_frontmatter(md: str) -> tuple[dict, str]:
    """Parse YAML frontmatter; return ({}, md) if absent or malformed."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", md, re.DOTALL)
    if not m:
        return {}, md
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}, md
    if not isinstance(fm, dict):
        return {}, md
    return fm, md[m.end():]


def _build_frontmatter_block(fm: dict, canonical_href: str) -> str:
    """
    Build the leading HTML-comment frontmatter block.

    The canonical: field is injected/overwritten so the HTML always knows
    where its source MD lives. YAML output is deterministic (sort_keys=True).
    """
    out = dict(fm)
    out["canonical"] = canonical_href
    yaml_body = yaml.safe_dump(
        out,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()
    return f"---\n{yaml_body}\n---\n"


def _canonical_href(source_path: Path) -> str:
    """`./<filename>.md` — relative to the rendered HTML's location."""
    return f"./{source_path.name}"


def _rewrite_wikilinks(md_text: str, source_path: Path, link_html: bool) -> str:
    """
    Rewrite [[slug]] and [[slug|alias]] into typed inline HTML anchors.

    mistune in escape=False mode passes inline HTML through unchanged, so
    the resulting <a> tags survive markdown rendering with all attributes
    intact. Targets are .md by default, .html when link_html=True (for
    full-graph projection runs).
    """
    suffix = ".html" if link_html else ".md"

    def repl(m: re.Match) -> str:
        raw = m.group(1).strip()
        target, _, alias = raw.partition("|")
        target = target.strip()
        alias = alias.strip() or target.rsplit("/", 1)[-1]
        if "/" in target:
            href = f"../{target}{suffix}"
        else:
            href = f"./{target}{suffix}"
        return f'<a href="{href}" data-relation="references">{alias}</a>'

    return WIKILINK_RE.sub(repl, md_text)


def _build_renderer() -> mistune.Markdown:
    """
    Deterministic markdown renderer.

    Plugins enabled: table, strikethrough, footnotes, task_lists.
    No auto-linking, no math (avoids client-side dependencies).
    """
    return mistune.create_markdown(
        escape=False,
        plugins=["table", "strikethrough", "footnotes", "task_lists"],
    )


def render_markdown_to_html(
    md_text: str,
    source_path: Path,
    link_html: bool = False,
) -> str:
    """
    Render a markdown string to a complete single-file HTML document.

    Args:
        md_text: Raw markdown including optional YAML frontmatter.
        source_path: Path of the source .md file; used for canonical link
            and title fallback (filename → title if no frontmatter title).
        link_html: If True, wikilinks resolve to sibling .html files (for
            full-graph projection). Default False → sibling .md targets.

    Returns:
        Complete HTML document as a string. Deterministic across runs.
    """
    fm, body_md = _split_frontmatter(md_text)
    body_md = _rewrite_wikilinks(body_md, source_path, link_html)
    canonical_href = _canonical_href(source_path)
    title = fm.get("title") or fm.get("slug") or source_path.stem
    body_html = _build_renderer()(body_md).rstrip()
    template = _load_template()
    css = _load_css()
    return template.safe_substitute(
        frontmatter_block=_build_frontmatter_block(fm, canonical_href),
        canonical_href=canonical_href,
        title=str(title),
        css=css,
        body_html=body_html,
    )
