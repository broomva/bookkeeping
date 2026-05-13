"""Tests for wikilink extraction (MD and HTML)."""
import pytest
from bookkeeping import extract_wikilinks_md


class TestExtractWikilinksMD:
    def test_single_wikilink(self):
        text = "See [[concept/foo]] for details."
        assert extract_wikilinks_md(text) == [("concept/foo", "references")]

    def test_multiple_wikilinks(self):
        text = "See [[concept/foo]] and [[pattern/bar]]."
        assert extract_wikilinks_md(text) == [
            ("concept/foo", "references"),
            ("pattern/bar", "references"),
        ]

    def test_pipe_alias_stripped(self):
        text = "See [[concept/foo|Foo Concept]]."
        assert extract_wikilinks_md(text) == [("concept/foo", "references")]

    def test_skip_html_comments(self):
        text = "Before <!-- [[ignored/link]] --> after [[real/link]]."
        assert extract_wikilinks_md(text) == [("real/link", "references")]

    def test_empty_text(self):
        assert extract_wikilinks_md("") == []

    def test_no_wikilinks(self):
        assert extract_wikilinks_md("Just plain prose.") == []
