"""Every rule, fired and not fired.

Rules are pure functions of a ParseResult, so most of these parse a generated
fixture with exactly one defect turned on and assert that exactly that rule
speaks up. A few build the state by hand where a fixture would be contrived.
"""

from __future__ import annotations

import dataclasses

import pytest

from gcode_lint.parser import parse_file, parse_stream
from gcode_lint.printers import resolve_printer
from gcode_lint.rules import (
    MATERIALS,
    RULE_INFO,
    RULES,
    Finding,
    LintConfig,
    at_or_above,
    grams_from_mm,
    rule_bed_adhesion,
    rule_build_volume,
    rule_chamber,
    rule_cold_extrusion,
    rule_end_retraction,
    rule_filament_remaining,
    rule_first_layer_fan,
    rule_first_layer_speed,
    rule_header_consistency,
    rule_temperature_range,
    rule_travel_retraction,
    rule_z_hop,
    run_rules,
    severity_rank,
)


def parse(text: str):
    return parse_stream(text.strip().splitlines(), source="inline")


# -- 1 first layer speed ---------------------------------------------------

def test_rule1_fires_when_the_first_layer_is_fast(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("fast", first_layer_speed=70.0)))
    findings = rule_first_layer_speed(state, mk4_config)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].line == state.first_layer.max_speed_line


def test_rule1_quiet_on_a_slow_first_layer(clean_state, mk4_config):
    assert rule_first_layer_speed(clean_state, mk4_config) == []


def test_rule1_respects_a_custom_threshold(build_gcode):
    state = parse_file(str(build_gcode("mid", first_layer_speed=45.0)))
    assert rule_first_layer_speed(state, LintConfig(first_layer_speed_max=40.0))
    assert rule_first_layer_speed(state, LintConfig(first_layer_speed_max=50.0)) == []


# -- 2 bed adhesion --------------------------------------------------------

def test_rule2_fires_without_a_skirt_or_purge(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("bare", skirt=False, purge_line=False)))
    findings = rule_bed_adhesion(state, mk4_config)
    assert len(findings) == 1
    assert findings[0].rule == "bed-adhesion"


def test_rule2_quiet_with_a_skirt(clean_state, mk4_config):
    assert rule_bed_adhesion(clean_state, mk4_config) == []


def test_rule2_accepts_a_purge_line_instead_of_a_skirt(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("purgeonly", skirt=False, purge_line=True)))
    assert rule_bed_adhesion(state, mk4_config) == []


# -- 3 temperature ---------------------------------------------------------

def test_rule3_fires_when_pla_runs_hot(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("hot", material="PLA", nozzle=250.0)))
    findings = rule_temperature_range(state, mk4_config)
    assert findings[0].severity == "high"
    assert "250" in findings[0].detail and "PLA" in findings[0].detail


def test_rule3_fires_when_petg_runs_cold(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("cold", material="PETG", nozzle=205.0, bed=75.0)))
    findings = rule_temperature_range(state, mk4_config)
    assert findings[0].severity == "high"
    assert "205" in findings[0].detail


def test_rule3_fires_on_a_bed_out_of_range(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("bed", material="PLA", bed=95.0)))
    findings = rule_temperature_range(state, mk4_config)
    assert [f.severity for f in findings] == ["medium"]
    assert "bed" in findings[0].title


def test_rule3_quiet_inside_the_window(clean_state, mk4_config):
    assert rule_temperature_range(clean_state, mk4_config) == []


def test_rule3_reports_an_undeclared_material_as_low(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("nomat", declare_material=False)))
    findings = rule_temperature_range(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "low"


@pytest.mark.parametrize("code", sorted(MATERIALS))
def test_every_material_has_a_usable_window(code):
    material = MATERIALS[code]
    assert material.nozzle_min < material.nozzle_max
    assert material.bed_min < material.bed_max
    assert 0.8 < material.density < 1.6


# -- 4 retraction on travel ------------------------------------------------

def test_rule4_fires_without_retraction(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("nortr", retract=False)))
    findings = rule_travel_retraction(state, mk4_config)
    assert len(findings) == 1
    assert findings[0].count > 5
    assert findings[0].lines


def test_rule4_quiet_when_travels_retract(clean_state, mk4_config):
    assert rule_travel_retraction(clean_state, mk4_config) == []


def test_rule4_ignores_travels_before_the_first_extrusion():
    state = parse(
        """
        G90
        M83
        G1 X10 Y10 F9000
        G1 X200 Y200 F9000
        G1 X200 Y201 E1.0 F1200
        """
    )
    assert rule_travel_retraction(state, LintConfig()) == []


def test_rule4_threshold_is_configurable(build_gcode):
    state = parse_file(str(build_gcode("nortr2", retract=False)))
    assert rule_travel_retraction(state, LintConfig(travel_retract_mm=3.0))
    assert rule_travel_retraction(state, LintConfig(travel_retract_mm=500.0)) == []


# -- 5 z hop ---------------------------------------------------------------

def test_rule5_fires_when_travel_drags_over_the_layer(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("nohop", z_hop=False)))
    findings = rule_z_hop(state, mk4_config)
    assert len(findings) == 1
    assert findings[0].count >= state.layer_count - 1


def test_rule5_quiet_with_z_hop(clean_state, mk4_config):
    assert rule_z_hop(clean_state, mk4_config) == []


def test_rule5_escalates_when_it_happens_constantly(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("nohop2", z_hop=False, layers=30)))
    assert rule_z_hop(state, mk4_config)[0].severity == "high"


# -- 6 first layer fan -----------------------------------------------------

def test_rule6_fires_for_abs_with_the_fan_on(build_gcode, mk4_config):
    state = parse_file(
        str(build_gcode("absfan", material="ABS", nozzle=250.0, bed=100.0,
                        chamber=50.0, fan_first_layer=100.0))
    )
    findings = rule_first_layer_fan(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "high"


def test_rule6_quiet_for_abs_with_the_fan_off(build_gcode, mk4_config):
    state = parse_file(
        str(build_gcode("absnofan", material="ABS", nozzle=250.0, bed=100.0,
                        chamber=50.0, fan_first_layer=0.0))
    )
    assert rule_first_layer_fan(state, mk4_config) == []


def test_rule6_quiet_for_pla_which_wants_cooling(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("plafan", material="PLA", fan_first_layer=100.0)))
    assert rule_first_layer_fan(state, mk4_config) == []


# -- 7 cold extrusion ------------------------------------------------------

def test_rule7_fires_when_nothing_sets_a_temperature(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("notemp", set_temp=False, wait_for_temp=False)))
    findings = rule_cold_extrusion(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "high"


def test_rule7_fires_when_nothing_waits(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("nowait", wait_for_temp=False)))
    findings = rule_cold_extrusion(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "medium"
    assert "M109" in findings[0].detail


def test_rule7_quiet_after_m109(clean_state, mk4_config):
    assert rule_cold_extrusion(clean_state, mk4_config) == []


# -- 8 build volume --------------------------------------------------------

def test_rule8_fires_when_the_model_leaves_the_bed(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("wide", x_offset=140.0)))
    findings = rule_build_volume(state, mk4_config)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].data["axis"] == "X"


def test_rule8_quiet_inside_the_volume(clean_state, mk4_config):
    assert rule_build_volume(clean_state, mk4_config) == []


def test_rule8_does_nothing_without_a_printer(build_gcode):
    state = parse_file(str(build_gcode("wide2", x_offset=140.0)))
    assert rule_build_volume(state, LintConfig(printer=None)) == []


def test_rule8_softens_for_an_end_gcode_park():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X10 Y10 E1.0 F1200
        G1 Z260.0 F600
        """
    )
    config = LintConfig(printer=resolve_printer("bambu-a1"), printer_source="--printer")
    findings = rule_build_volume(state, config)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert "end gcode" in findings[0].detail


def test_rule8_catches_negative_coordinates():
    state = parse(
        """
        G90
        M83
        G1 X-5 Y10 F3000
        G1 X10 Y10 E1.0 F1200
        """
    )
    config = LintConfig(printer=resolve_printer("ender3"), printer_source="--printer")
    findings = rule_build_volume(state, config)
    assert any("below the origin" in finding.detail for finding in findings)


# -- 9 end retraction ------------------------------------------------------

def test_rule9_fires_when_the_end_gcode_never_retracts(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("noend", end_retract=False)))
    findings = rule_end_retraction(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "medium"


def test_rule9_quiet_when_it_does(clean_state, mk4_config):
    assert rule_end_retraction(clean_state, mk4_config) == []


def test_rule9_notes_a_retraction_that_comes_too_late():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X10 Y10 E1.0 F1200
        G1 X200 Y200 F3000
        G1 E-2.0 F2400
        """
    )
    findings = rule_end_retraction(state, LintConfig())
    assert len(findings) == 1 and findings[0].severity == "low"


# -- 10 chamber ------------------------------------------------------------

def test_rule10_fires_for_abs_with_no_chamber(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("abs", material="ABS", nozzle=250.0, bed=100.0)))
    findings = rule_chamber(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "medium"


def test_rule10_quiet_when_a_chamber_is_set(build_gcode, mk4_config):
    state = parse_file(
        str(build_gcode("abschamber", material="ABS", nozzle=250.0, bed=100.0, chamber=50.0))
    )
    assert rule_chamber(state, mk4_config) == []


def test_rule10_quiet_for_pla(clean_state, mk4_config):
    assert rule_chamber(clean_state, mk4_config) == []


def test_rule10_softens_on_an_enclosed_printer(build_gcode):
    state = parse_file(str(build_gcode("absp1s", material="ABS", nozzle=250.0, bed=100.0)))
    config = LintConfig(printer=resolve_printer("bambu-p1s"), printer_source="--printer")
    assert rule_chamber(state, config)[0].severity == "low"


# -- 11 filament remaining -------------------------------------------------

def test_rule11_fires_when_the_spool_is_too_light(clean_state, mk4_config):
    config = dataclasses.replace(mk4_config, remaining_g=0.2)
    findings = rule_filament_remaining(clean_state, config)
    assert len(findings) == 1 and findings[0].severity == "high"
    assert findings[0].data["runs_out_layer"] is not None


def test_rule11_quiet_with_plenty_left(clean_state, mk4_config):
    config = dataclasses.replace(mk4_config, remaining_g=800.0)
    assert rule_filament_remaining(clean_state, config) == []


def test_rule11_warns_on_a_thin_margin(clean_state, mk4_config):
    needed = grams_from_mm(clean_state.totals.extrusion_mm, 1.75, 1.24)
    config = dataclasses.replace(mk4_config, remaining_g=needed + 0.05)
    findings = rule_filament_remaining(clean_state, config)
    assert len(findings) == 1 and findings[0].severity == "low"


def test_rule11_does_nothing_without_remaining(clean_state, mk4_config):
    assert rule_filament_remaining(clean_state, mk4_config) == []


def test_rule11_uses_the_header_total_when_it_agrees(clean_state, mk4_config):
    from gcode_lint.rules import estimated_grams

    needed, source = estimated_grams(clean_state, mk4_config)
    assert source == "slicer header"
    assert needed == pytest.approx(clean_state.header.estimated_filament_g, abs=0.2)


def test_rule11_ignores_a_stale_header_total(build_gcode, mk4_config):
    from gcode_lint.rules import estimated_grams

    state = parse_file(str(build_gcode("stalespool", header_filament_mm=9000.0)))
    needed, source = estimated_grams(state, mk4_config)
    assert "stale" in source
    assert needed == pytest.approx(
        grams_from_mm(state.totals.extrusion_mm, 1.75, 1.24), abs=0.01
    )


def test_grams_from_mm_matches_the_cylinder_volume():
    # 1000 mm of 1.75 mm PLA is 2.405 cm3, which is 2.98 g at 1.24 g/cm3.
    assert grams_from_mm(1000.0, 1.75, 1.24) == pytest.approx(2.98, abs=0.01)


# -- 12 header consistency -------------------------------------------------

def test_rule12_fires_when_the_header_is_stale(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("stale", header_filament_mm=4000.0)))
    findings = rule_header_consistency(state, mk4_config)
    assert len(findings) == 1 and findings[0].severity == "medium"
    assert findings[0].data["difference_pct"] > 10


def test_rule12_quiet_when_the_header_agrees(clean_state, mk4_config):
    assert rule_header_consistency(clean_state, mk4_config) == []


def test_rule12_reports_a_header_with_no_totals():
    state = parse(
        """
        G90
        M83
        G1 Z0.2 F720
        G1 X10 Y10 E1.0 F1200
        """
    )
    findings = rule_header_consistency(state, LintConfig())
    assert len(findings) == 1 and findings[0].severity == "low"


def test_rule12_tolerates_small_differences(build_gcode, mk4_config):
    state = parse_file(str(build_gcode("near")))
    declared = state.header.estimated_filament_mm
    assert abs(declared - state.totals.extrusion_mm) / declared < 0.10
    assert rule_header_consistency(state, mk4_config) == []


# -- the engine itself -----------------------------------------------------

def test_a_clean_file_produces_no_findings(clean_state, mk4_config):
    assert run_rules(clean_state, mk4_config) == []


def test_findings_come_back_worst_first(build_gcode, mk4_config):
    state = parse_file(
        str(build_gcode("messy", material="ABS", nozzle=250.0, bed=100.0,
                        first_layer_speed=80.0, fan_first_layer=100.0, z_hop=False))
    )
    findings = run_rules(state, mk4_config)
    ranks = [severity_rank(finding.severity) for finding in findings]
    assert ranks == sorted(ranks)
    assert len(findings) >= 4


def test_every_rule_is_listed_in_rule_info():
    assert len(RULES) == len(RULE_INFO) == 12
    assert [number for number, _, _ in RULE_INFO] == list(range(1, 13))


def test_findings_always_carry_the_four_facts(build_gcode, mk4_config):
    state = parse_file(
        str(build_gcode("all", material="ABS", nozzle=300.0, bed=130.0,
                        first_layer_speed=90.0, fan_first_layer=100.0,
                        retract=False, z_hop=False, skirt=False, purge_line=False,
                        end_retract=False, x_offset=140.0))
    )
    findings = run_rules(state, dataclasses.replace(mk4_config, remaining_g=0.1))
    assert len(findings) >= 8
    for finding in findings:
        assert finding.severity in ("high", "medium", "low")
        assert finding.detail and finding.why and finding.fix
        assert finding.title
        assert finding.number in range(1, 13)


@pytest.mark.parametrize("grams,expected", [(0.5, "0.5 g"), (9.94, "9.9 g"), (240.0, "240 g")])
def test_gram_formatting(grams, expected):
    from gcode_lint.rules import format_grams

    assert format_grams(grams) == expected


def test_severity_comparison():
    assert at_or_above("high", "medium") is True
    assert at_or_above("low", "medium") is False
    assert at_or_above("medium", "medium") is True
    assert at_or_above("low", "low") is True


def test_finding_serialises_to_a_dict():
    finding = Finding(
        rule="demo", number=1, severity="low", title="t", detail="d", why="w", fix="f",
        line=12,
    )
    payload = finding.to_dict()
    assert payload["rule"] == "demo" and payload["line"] == 12
    assert set(payload) >= {"severity", "detail", "why", "fix", "count", "lines"}
