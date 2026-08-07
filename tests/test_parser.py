"""Parser mechanics: modal state, motion classification and layer detection."""

from __future__ import annotations

import pytest

import fixtures
from gcode_lint.parser import ParseError, parse_file, parse_stream


def parse(text: str, **kwargs):
    return parse_stream(text.strip().splitlines(), source="inline", **kwargs)


def test_relative_extrusion_totals():
    state = parse(
        """
        G90
        M83
        G1 X0 Y0 F3000
        G1 X10 Y0 E1.0 F1200
        G1 X20 Y0 E1.0 F1200
        """
    )
    assert state.totals.extrusion_mm == pytest.approx(2.0)
    assert state.relative_e is True


def test_absolute_extrusion_totals():
    state = parse(
        """
        G90
        M82
        G1 X0 Y0 F3000
        G1 X10 Y0 E1.0 F1200
        G1 X20 Y0 E2.0 F1200
        """
    )
    assert state.totals.extrusion_mm == pytest.approx(2.0)
    assert state.relative_e is False


def test_g92_resets_the_extruder_position():
    state = parse(
        """
        G90
        M82
        G1 X10 Y0 E5.0 F1200
        G92 E0
        G1 X20 Y0 E5.0 F1200
        """
    )
    assert state.totals.extrusion_mm == pytest.approx(10.0)


def test_relative_positioning():
    state = parse(
        """
        G90
        M83
        G1 X10 Y10 F3000
        G91
        G1 X10 Y0 E1 F1200
        """
    )
    assert state.x_max.value == pytest.approx(20.0)


def test_retraction_is_not_counted_as_extrusion():
    state = parse(
        """
        G90
        M83
        G1 X10 Y0 E1.0 F1200
        G1 E-0.8 F2400
        G1 X40 Y0 F9000
        G1 E0.8 F2400
        G1 X50 Y0 E1.0 F1200
        """
    )
    assert state.totals.extrusion_mm == pytest.approx(2.0)
    assert state.totals.retract_mm == pytest.approx(0.8)


def test_travel_after_retraction_is_marked_retracted():
    state = parse(
        """
        G90
        M83
        G1 X10 Y0 E1.0 F1200
        G1 E-0.8 F2400
        G1 X60 Y0 F9000
        """
    )
    travels = [event for event in state.travel_events if event.length > 10]
    assert all(event.retracted for event in travels)


def test_travel_without_retraction_is_recorded():
    state = parse(
        """
        G90
        M83
        G1 X10 Y0 E1.0 F1200
        G1 X60 Y0 F9000
        """
    )
    events = [event for event in state.travel_events if event.length > 10]
    assert len(events) == 1
    assert events[0].retracted is False


def test_z_hop_is_detected_around_a_travel():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X10 Y0 E1.0 F1200
        G1 Z0.6 F9000
        G1 X60 Y0 F9000
        G1 Z0.2 F9000
        """
    )
    events = [event for event in state.travel_events if event.length > 10]
    assert events[0].z_hop is True


def test_travel_crossing_printed_material_is_flagged():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X0 Y0 F9000
        G1 X60 Y0 E2.0 F1200
        G1 X60 Y1 E0.1 F1200
        G1 X0 Y1 E2.0 F1200
        G1 X60 Y0.5 F9000
        """
    )
    crossing = [event for event in state.travel_events if event.crossed_print]
    assert crossing, "the travel back across the two printed lines was missed"


def test_travel_over_empty_bed_is_not_flagged_as_crossing():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X0 Y0 F9000
        G1 X20 Y0 E1.0 F1200
        G1 X20 Y80 F9000
        """
    )
    assert not any(event.crossed_print for event in state.travel_events)


def test_layers_come_from_markers(build_gcode):
    state = parse_file(str(build_gcode("layers", layers=5)))
    assert [layer.index for layer in state.layers] == [1, 2, 3, 4, 5]
    assert state.layers[0].z == pytest.approx(0.2)
    assert state.layers[4].z == pytest.approx(1.0)


def test_layers_are_inferred_from_z_when_no_markers_exist():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X10 Y0 E1.0 F1200
        G1 Z0.4 F720
        G1 X20 Y0 E1.0 F1200
        G1 Z0.6 F720
        G1 X30 Y0 E1.0 F1200
        """
    )
    assert state.layer_count == 3
    assert state.saw_layer_marker is False


def test_start_gcode_extrusion_is_kept_out_of_layer_one(build_gcode):
    state = parse_file(str(build_gcode("purge")))
    assert state.start_gcode is not None
    assert state.start_gcode_extrusion_mm > 5.0
    assert state.start_gcode_extrusion_mm < state.totals.extrusion_mm
    assert state.layers[0].index == 1


def test_bounds_track_the_extremes_and_their_lines():
    state = parse(
        """
        G90
        M83
        G1 X10 Y20 F3000
        G1 X250 Y30 F3000
        G1 X5 Y5 F3000
        G1 Z18.5 F720
        """
    )
    assert state.x_max.value == pytest.approx(250.0)
    assert state.x_max.line == 4
    assert state.x_min.value == pytest.approx(5.0)
    assert state.z_max.value == pytest.approx(18.5)


def test_arc_moves_are_counted_and_extrude():
    state = parse(
        """
        G90
        M83
        G1 X0 Y0 F3000
        G2 X10 Y10 I5 J0 E1.0 F1200
        """
    )
    assert state.totals.arcs == 1
    assert state.totals.extrusion_mm == pytest.approx(1.0)


def test_dwell_adds_to_the_time_estimate():
    state = parse(
        """
        G90
        G4 P1500
        G4 S2
        """
    )
    assert state.totals.time_s == pytest.approx(3.5)


def test_homing_resets_position():
    state = parse(
        """
        G90
        G1 X100 Y100 F3000
        G28
        G1 X10 Y10 F3000
        """
    )
    assert state.homed is True
    assert state.x_min.value == pytest.approx(10.0)


def test_auxiliary_fan_is_ignored():
    state = parse(
        """
        M106 P2 S255
        M106 P1 S128
        """
    )
    assert len(state.fan_events) == 1
    assert state.fan_events[0].percent == pytest.approx(50.2, abs=0.2)


def test_temperature_extremes_are_exact():
    state = parse(
        """
        M104 S200
        M109 S260
        M104 S180
        M140 S60
        M190 S100
        """
    )
    assert state.nozzle_max.value == 260
    assert state.nozzle_min.value == 180
    assert state.bed_max.value == 100
    assert state.bed_min.value == 60


def test_firmware_retraction_is_understood():
    state = parse(
        """
        G90
        M83
        G1 X10 Y0 E1.0 F1200
        G10
        G1 X60 Y0 F9000
        """
    )
    assert state.end.retracted_at_end is True
    assert all(event.retracted for event in state.travel_events if event.length > 10)


def test_inch_units_are_flagged():
    state = parse("G20\nG28")
    assert state.inch_units is True


def test_evidence_lists_are_capped():
    body = ["G90", "M83", "G1 Z0.2 F720", "G1 X0 Y0 E1 F1200"]
    for index in range(200):
        body.append("G1 X%d Y50 F9000" % (index % 100))
        body.append("G1 X%d Y60 E0.5 F1200" % (index % 100))
    state = parse_stream(body, max_events=5)
    assert len(state.travel_events) == 5
    assert state.travel_events_dropped > 0


def test_features_split_into_adhesion_and_object():
    state = parse(
        """
        G90
        M83
        ;TYPE:Skirt
        G1 X10 Y0 E1 F1200
        ;TYPE:External perimeter
        G1 X20 Y0 E1 F1200
        """
    )
    assert state.adhesion_feature == "Skirt"
    assert state.first_object_feature == "External perimeter"


def test_orca_feature_comments_are_read():
    state = parse(
        """
        G90
        M83
        ; FEATURE: Skirt
        G1 X10 Y0 E1 F1200
        ; FEATURE: Outer wall
        G1 X20 Y0 E1 F1200
        """
    )
    assert state.adhesion_feature == "Skirt"
    assert state.first_object_feature == "Outer wall"


def test_parse_stream_accepts_a_generator():
    def lines():
        yield "G90"
        yield "M83"
        yield "G1 X10 Y0 E1 F1200"

    state = parse_stream(lines())
    assert state.totals.extrusion_mm == pytest.approx(1.0)


def test_empty_file_is_a_parse_error(tmp_path):
    path = tmp_path / "empty.gcode"
    path.write_text("")
    with pytest.raises(ParseError):
        parse_file(str(path))


def test_comment_only_file_is_a_parse_error(tmp_path):
    path = tmp_path / "notes.gcode"
    path.write_text("; just a note\n; and another\n")
    with pytest.raises(ParseError):
        parse_file(str(path))


def test_missing_file_is_a_parse_error(tmp_path):
    with pytest.raises(ParseError):
        parse_file(str(tmp_path / "nope.gcode"))


def test_binary_content_is_a_parse_error(tmp_path):
    path = tmp_path / "blob.gcode"
    path.write_bytes(b"G1 X10\x00\x01\x02binary\n")
    with pytest.raises(ParseError):
        parse_file(str(path))


def test_binary_gcode_magic_is_reported_clearly(tmp_path):
    path = tmp_path / "part.bgcode"
    path.write_bytes(b"GCDE\x00\x01\x02\x03rest")
    with pytest.raises(ParseError) as excinfo:
        parse_file(str(path))
    assert "bgcode" in str(excinfo.value)


def test_line_numbers_are_one_based(build_gcode):
    path = build_gcode("lines")
    state = parse_file(str(path))
    assert state.line_count == len(path.read_text().splitlines())
