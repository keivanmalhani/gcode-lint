"""The installed console_scripts entry point."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from gcode_lint import __version__


def _console_scripts():
    return [ep for ep in entry_points(group="console_scripts") if ep.name == "gcode-lint"]


def _script_path():
    """The installed gcode-lint executable, on PATH or beside the interpreter."""
    found = shutil.which("gcode-lint")
    if found:
        return found
    beside = Path(sys.executable).parent / "gcode-lint"
    return str(beside) if beside.exists() else None


def test_entry_point_is_registered():
    scripts = _console_scripts()
    assert scripts, "gcode-lint is not installed as a console script"
    assert scripts[0].value == "gcode_lint.cli:main"


def test_entry_point_loads_and_is_callable():
    loaded = _console_scripts()[0].load()
    assert callable(loaded)
    from gcode_lint.cli import main

    assert loaded is main


def test_installed_script_reports_its_version():
    executable = _script_path()
    assert executable, "gcode-lint is not installed; run pip install -e \".[dev]\""
    result = subprocess.run([executable, "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_installed_script_lints_a_file(build_gcode):
    executable = _script_path()
    assert executable
    path = str(build_gcode("entry", nozzle=250.0))
    result = subprocess.run(
        [executable, "check", path, "--printer", "prusa-mk4"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "temperature-range" in result.stdout


def test_module_can_be_run_with_dash_m(build_gcode):
    result = subprocess.run(
        [sys.executable, "-m", "gcode_lint.cli", "check", str(build_gcode("m")),
         "--printer", "prusa-mk4"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "no findings" in result.stdout
