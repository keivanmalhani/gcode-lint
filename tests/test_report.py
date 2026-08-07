"""Text, JSON and stats rendering."""

from __future__ import annotations

import dataclasses
import json

import pytest

from gcode_lint import __version__
from gcode_lint.parser import parse_file
from gcode_lint.report import (
    build_payload,
    counts,
    exit_code,
    format_finding,
    format_grams,
    notes,
    render_json,
    render_stats,
    render_text,
)
from gcode_lint.rules import Finding, run_rules


def _findings(state, config):
    return run_rules(state, config)


def test_clean_report_says_so(clean_state, mk4_config):
    text = render_text(clean_state, [], mk4_config)
    assert "no findings" in text
    assert text.startswith("gcode-lint %s" % __version__)
    assert "clean," in text


def test_report_header_carries_the_facts(clean_state, mk4_config):
    text = render_text(clean_state, [], mk4_config)
    for label in ("slicer", "material", "printer", "layers", "filament", "time"):
        assert "\n%s" % label in text or text.startswith(label)
    assert "PrusaSlicer" in text and "PLA" in text and "Prusa MK4" in text


def test_findings_render_with_what_why_and_fix(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("hot", nozzle=250.0)))
    text = render_text(state, _findings(state, mk4_config), mk4_config)
    assert "what" in text and "why" in text and "fix" in text
    assert "temperature-range" in text


def test_finding_block_shows_the_line_number():
    finding = Finding(
        rule="demo", number=3, severity="high", title="a title", detail="d", why="w",
        fix="f", line=42,
    )
    assert "line 42" in format_finding(finding)


def test_finding_block_summarises_many_lines():
    finding = Finding(
        rule="demo", number=4, severity="medium", title="t", detail="d", why="w", fix="f",
        line=1, count=30, lines=(1, 2, 3), truncated=True,
    )
    rendered = format_finding(finding)
    assert "lines 1, 2, 3 and 27 more" in rendered


def test_finding_without_a_line_says_whole_file():
    finding = Finding(
        rule="demo", number=10, severity="low", title="t", detail="d", why="w", fix="f",
    )
    assert "whole file" in format_finding(finding)


def test_counts_tally_by_severity():
    findings = [
        Finding(rule="a", number=1, severity="high", title="t", detail="d", why="w", fix="f"),
        Finding(rule="b", number=2, severity="low", title="t", detail="d", why="w", fix="f"),
        Finding(rule="c", number=3, severity="low", title="t", detail="d", why="w", fix="f"),
    ]
    assert counts(findings) == {"high": 1, "medium": 0, "low": 2}


@pytest.mark.parametrize(
    "severity,fail_on,expected",
    [
        ("high", "high", 1),
        ("medium", "high", 0),
        ("medium", "medium", 1),
        ("low", "medium", 0),
        ("low", "low", 1),
    ],
)
def test_exit_code_follows_fail_on(severity, fail_on, expected):
    findings = [
        Finding(rule="a", number=1, severity=severity, title="t", detail="d", why="w", fix="f")
    ]
    assert exit_code(findings, fail_on) == expected


def test_exit_code_is_zero_with_no_findings():
    assert exit_code([], "low") == 0


def test_notes_mention_a_missing_printer(clean_state, mk4_config):
    without = dataclasses.replace(mk4_config, printer=None, printer_source="none")
    assert any("build volume" in note for note in notes(clean_state, without))
    assert not any("build volume" in note for note in notes(clean_state, mk4_config))


def test_notes_mention_a_printer_taken_from_the_header(clean_state, mk4_config):
    from_header = dataclasses.replace(mk4_config, printer_source="header")
    assert any("slicer header" in note for note in notes(clean_state, from_header))


def test_stats_table_has_one_row_per_layer(clean_state):
    rows = render_stats(clean_state)
    body = [row for row in rows if row and row[0].isdigit()]
    assert len(body) == clean_state.layer_count
    assert "max speed" in rows[0]


def test_stats_appear_in_the_text_report(clean_state, mk4_config):
    text = render_text(clean_state, [], mk4_config, stats=True)
    assert "max speed" in text
    assert "total" in text


def test_json_payload_is_valid_and_complete(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("json", nozzle=250.0)))
    payload = json.loads(render_json(state, _findings(state, mk4_config), mk4_config))
    assert payload["tool"] == "gcode-lint"
    assert payload["version"] == __version__
    assert payload["slicer"]["name"] == "PrusaSlicer"
    assert payload["printer"]["key"] == "prusa-mk4"
    assert payload["summary"]["layers"] == state.layer_count
    assert payload["findings"] and payload["findings"][0]["rule"] == "temperature-range"
    assert payload["exit_code"] == 1


def test_json_includes_layers_only_with_stats(clean_state, mk4_config):
    without = build_payload(clean_state, [], mk4_config, stats=False)
    with_stats = build_payload(clean_state, [], mk4_config, stats=True)
    assert "layers" not in without
    assert len(with_stats["layers"]) == clean_state.layer_count
    assert with_stats["layers"][0]["max_speed_mm_s"] > 0


def test_json_reports_the_spool_when_given(clean_state, mk4_config):
    config = dataclasses.replace(mk4_config, remaining_g=240.0)
    payload = build_payload(clean_state, [], config)
    assert payload["summary"]["remaining_g"] == 240.0
    assert payload["summary"]["needed_g"] > 0


@pytest.mark.parametrize("grams,expected", [(0.5, "0.5 g"), (9.94, "9.9 g"), (240.0, "240 g")])
def test_gram_formatting(grams, expected):
    assert format_grams(grams) == expected
