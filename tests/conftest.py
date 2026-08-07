"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fixtures as gcode_fixtures  # noqa: E402


@pytest.fixture
def build_gcode(tmp_path):
    """Write a generated gcode file and return its path."""
    counter = {"n": 0}

    def make(name: str = "part", **kwargs) -> Path:
        counter["n"] += 1
        path = tmp_path / ("%s_%d.gcode" % (name, counter["n"]))
        gcode_fixtures.write(path, **kwargs)
        return path

    return make


@pytest.fixture
def clean_state(build_gcode):
    """A parsed file that no rule complains about."""
    from gcode_lint.parser import parse_file

    return parse_file(str(build_gcode("clean")))


@pytest.fixture
def mk4_config():
    from gcode_lint.printers import resolve_printer
    from gcode_lint.rules import LintConfig

    return LintConfig(printer=resolve_printer("prusa-mk4"), printer_source="--printer")
