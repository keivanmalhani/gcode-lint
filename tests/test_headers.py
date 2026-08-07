"""Header parsing for each slicer dialect.

The four supported slicers write the same facts in four different shapes, so
each of these runs against all four generated dialects.
"""

from __future__ import annotations

import pytest

import fixtures
from gcode_lint.parser import normalise_material, parse_duration, parse_file, parse_stream

EXPECTED_SLICER = {
    "prusaslicer": "PrusaSlicer",
    "cura": "Cura",
    "orcaslicer": "OrcaSlicer",
    "bambustudio": "Bambu Studio",
}


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_slicer_name_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.header.slicer == EXPECTED_SLICER[dialect]


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_slicer_version_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.header.slicer_version
    assert state.header.slicer_version[0].isdigit()


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_material_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect, material="PETG")))
    assert state.header.material == "PETG"


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_printer_model_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.header.printer_model == fixtures.PRINTER_MODEL[dialect]


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_estimated_time_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.header.estimated_time_s and state.header.estimated_time_s > 0


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_filament_length_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    declared = state.header.estimated_filament_mm
    assert declared is not None
    assert abs(declared - state.totals.extrusion_mm) < 5.0


@pytest.mark.parametrize("dialect", ["prusaslicer", "orcaslicer", "bambustudio"])
def test_filament_weight_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.header.estimated_filament_g and state.header.estimated_filament_g > 0


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_temperatures_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect, nozzle=225, bed=65)))
    assert state.header.nozzle_temp == 225
    assert state.header.bed_temp == 65


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_layer_count_detected(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect, layers=6)))
    assert state.header.layer_count == 6
    assert state.layer_count == 6


@pytest.mark.parametrize("dialect", fixtures.DIALECTS)
def test_layer_markers_are_recognised(build_gcode, dialect):
    state = parse_file(str(build_gcode(dialect, dialect=dialect)))
    assert state.saw_layer_marker is True


def test_bambu_two_facts_on_one_comment_line():
    """Bambu Studio puts model time and total time on the same line."""
    state = parse_stream(
        [
            "; model printing time: 1h 3m 5s; total estimated time: 1h 10m 0s",
            "G28",
        ]
    )
    assert state.header.estimated_time_s == 4200.0


def test_cura_filament_used_is_metres():
    state = parse_stream([";FLAVOR:Marlin", ";Filament used: 4.12345m", "G28"])
    assert state.header.estimated_filament_mm == pytest.approx(4123.45)


def test_m73_is_only_a_fallback_for_time():
    state = parse_stream(["M73 P0 R120", "G28"])
    assert state.header.estimated_time_s == 7200.0


def test_header_time_beats_m73():
    state = parse_stream(
        ["M73 P0 R120", "G28", "; estimated printing time (normal mode) = 1h 0m 0s"]
    )
    assert state.header.estimated_time_s == 3600.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PLA", "PLA"),
        ("Generic PLA", "PLA"),
        ("Bambu PLA Basic @BBL X1C", "PLA"),
        ("PolyLite PLA+", "PLA"),
        ("Prusament PETG", "PETG"),
        ("Generic ABS", "ABS"),
        ("Nylon", "PA"),
        ("PA6-CF", "PA"),
        ("support material", None),
        ("", None),
    ],
)
def test_material_names_reduce_to_codes(raw, expected):
    assert normalise_material(raw) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1h 2m 3s", 3723.0),
        ("45s", 45.0),
        ("2d 1h", 176400.0),
        ("4523", 4523.0),
        ("13m 5s", 785.0),
    ],
)
def test_duration_parsing(text, expected):
    assert parse_duration(text) == expected


def test_unknown_slicer_leaves_name_empty():
    state = parse_stream(["; sliced by something else", "G28"])
    assert state.header.slicer is None
    assert state.header.describe_slicer() == "unknown slicer"
