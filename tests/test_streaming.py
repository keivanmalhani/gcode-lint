"""The file is read once, line by line, and never held in memory.

These run against a generated file of more than half a million lines. It is
built into tmp_path rather than committed, so nothing large lives in the repo.
"""

from __future__ import annotations

import time
import tracemalloc

import pytest

import fixtures
from gcode_lint.parser import MAX_EVENTS, parse_file, parse_stream
from gcode_lint.printers import resolve_printer
from gcode_lint.report import render_text
from gcode_lint.rules import LintConfig, run_rules

TARGET_LINES = 500_000


@pytest.fixture(scope="module")
def big_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / "huge.gcode"
    written = fixtures.large_file(path, TARGET_LINES)
    assert written >= TARGET_LINES
    return path


@pytest.fixture(scope="module")
def big_state(big_file):
    """Parsed once and shared: the assertions below are about one parse."""
    return parse_file(str(big_file))


def test_the_generated_file_really_is_large(big_file):
    assert big_file.stat().st_size > 10 * 1024 * 1024
    with open(big_file, encoding="ascii") as handle:
        assert sum(1 for _ in handle) >= TARGET_LINES


def test_parsing_half_a_million_lines(big_state):
    assert big_state.line_count >= TARGET_LINES
    assert big_state.layer_count > 100
    assert big_state.totals.extrusion_mm > 0
    assert big_state.header.material == "PLA"
    assert big_state.header.slicer == "PrusaSlicer"


def test_peak_memory_stays_far_below_the_file_size(big_file):
    """The proof that the parser streams: peak allocation is a rounding error
    next to the file, which would be tens of megabytes if it were read whole."""
    size = big_file.stat().st_size
    tracemalloc.start()
    try:
        parse_file(str(big_file))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < size * 0.05
    assert peak < 4 * 1024 * 1024


def test_evidence_lists_stay_bounded_on_a_large_file(big_state):
    for events in (
        big_state.travel_events,
        big_state.temp_events,
        big_state.fan_events,
        big_state.cold_extrude_events,
    ):
        assert len(events) <= MAX_EVENTS
    # The file really does contain more than the cap, so the cap is doing work.
    assert big_state.travel_events_dropped > 1000
    assert len(big_state.features_seen) <= MAX_EVENTS


def test_a_large_file_parses_in_reasonable_time(big_file):
    start = time.monotonic()
    parse_file(str(big_file))
    assert time.monotonic() - start < 60.0


def test_the_whole_pipeline_runs_on_a_large_file(big_state):
    """Rules and rendering work off the parse result, not the file."""
    config = LintConfig(printer=resolve_printer("prusa-mk4"), printer_source="--printer")
    findings = run_rules(big_state, config)
    text = render_text(big_state, findings, config)
    assert "gcode-lint" in text
    assert any(finding.rule == "travel-retraction" for finding in findings)


def test_parse_stream_never_needs_the_whole_iterable():
    """A one shot generator is enough, so nothing indexes or re-reads it."""

    def lines():
        yield "G90"
        yield "M83"
        yield "M109 S215"
        for index in range(20_000):
            yield "G1 X%.2f Y%.2f E0.05 F1800" % (index % 100, index % 90)

    source = lines()
    state = parse_stream(source)
    assert state.totals.moves == 20_000
    assert next(source, None) is None
