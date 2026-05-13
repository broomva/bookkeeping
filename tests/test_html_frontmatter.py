"""Tests for HTML frontmatter parsing (Category B/C support)."""
import pytest
from bookkeeping import parse_html_frontmatter


class TestParseHTMLFrontmatter:
    def test_valid_html_comment_frontmatter(self):
        html = """<!DOCTYPE html>
<!--
---
type: synthesis
slug: html-vs-md
score: 7
---
-->
<html><body>Hello</body></html>
"""
        fm, body = parse_html_frontmatter(html)
        assert fm == {"type": "synthesis", "slug": "html-vs-md", "score": 7}
        assert "<html><body>Hello</body></html>" in body

    def test_missing_frontmatter_returns_empty(self):
        html = "<!DOCTYPE html>\n<html><body>No frontmatter</body></html>"
        fm, body = parse_html_frontmatter(html)
        assert fm == {}
        assert body == html

    def test_empty_string(self):
        fm, body = parse_html_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_malformed_yaml_returns_empty(self):
        html = """<!DOCTYPE html>
<!--
---
type: synthesis
  invalid: : :
---
-->
<html></html>
"""
        fm, body = parse_html_frontmatter(html)
        assert fm == {}

    def test_frontmatter_only_in_first_4kb(self):
        padding = "<!-- padding -->\n" * 500
        html = f"<!DOCTYPE html>\n{padding}<!--\n---\ntype: late\n---\n-->\n<html></html>"
        fm, _ = parse_html_frontmatter(html)
        assert fm == {}

    def test_list_frontmatter_value(self):
        html = """<!DOCTYPE html>
<!--
---
type: synthesis
related_entities:
  - concept/foo
  - pattern/bar
---
-->
<html></html>
"""
        fm, _ = parse_html_frontmatter(html)
        assert fm["related_entities"] == ["concept/foo", "pattern/bar"]
