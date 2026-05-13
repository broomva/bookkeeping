"""Tests for bookkeeping render subcommand."""
import pytest
from pathlib import Path
from render import render_markdown_to_html


class TestRenderCore:
    def test_basic_md_to_html(self):
        md = "---\ntitle: Hello\n---\n# Heading\n\nParagraph."
        html = render_markdown_to_html(md, source_path=Path("note.md"))
        assert "<!DOCTYPE html>" in html
        assert "<h1>Heading</h1>" in html
        assert "<p>Paragraph.</p>" in html
        assert "<style>" in html

    def test_title_from_frontmatter(self):
        md = "---\ntitle: Custom Title\n---\nBody."
        html = render_markdown_to_html(md, source_path=Path("note.md"))
        assert "<title>Custom Title</title>" in html

    def test_title_fallback_to_slug(self):
        md = "---\nslug: my-slug\n---\nBody."
        html = render_markdown_to_html(md, source_path=Path("note.md"))
        assert "<title>my-slug</title>" in html

    def test_canonical_link_present(self):
        md = "Body."
        html = render_markdown_to_html(md, source_path=Path("research/notes/x.md"))
        assert 'rel="canonical"' in html
        assert "x.md" in html

    def test_frontmatter_in_comment(self):
        md = "---\ntype: synthesis\nscore: 7\n---\nBody."
        html = render_markdown_to_html(md, source_path=Path("note.md"))
        assert "<!--" in html
        assert "type: synthesis" in html
        assert "score: 7" in html
        assert "canonical:" in html  # injected

    def test_determinism(self):
        md = "---\ntitle: T\n---\n# H\n\nP."
        h1 = render_markdown_to_html(md, source_path=Path("note.md"))
        h2 = render_markdown_to_html(md, source_path=Path("note.md"))
        h3 = render_markdown_to_html(md, source_path=Path("note.md"))
        assert h1 == h2 == h3
