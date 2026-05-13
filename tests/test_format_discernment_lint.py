"""Tests for the four format-discernment lint checks."""
import os
import time
from pathlib import Path
import pytest
from bookkeeping import lint_format_discernment


class TestStaleProjection:
    def test_clean(self, tmp_path):
        md = tmp_path / "x-synthesis.md"
        html = tmp_path / "x-synthesis.html"
        md.write_text("---\nslug: x\n---\nBody")
        html.write_text("<!DOCTYPE html><html></html>")
        future = time.time() + 10
        os.utime(html, (future, future))
        errors = lint_format_discernment(tmp_path)
        assert [e for e in errors if e.field == "stale_projection"] == []

    def test_stale(self, tmp_path):
        md = tmp_path / "x-synthesis.md"
        html = tmp_path / "x-synthesis.html"
        md.write_text("---\nslug: x\n---\nBody")
        html.write_text("<!DOCTYPE html><html></html>")
        past = time.time() - 10
        os.utime(html, (past, past))
        errors = lint_format_discernment(tmp_path)
        stale = [e for e in errors if e.field == "stale_projection"]
        assert len(stale) == 1
        assert stale[0].severity == "warning"
        assert "x-synthesis.html" in stale[0].file_path
