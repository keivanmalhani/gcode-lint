"""The command line: arguments, output and exit codes."""

from __future__ import annotations

import json

import pytest

from gcode_lint import __version__
from gcode_lint.cli import EXIT_CLEAN, EXIT_FINDINGS, EXIT_UNPARSEABLE, main, parse_mass


def test_clean_file_exits_zero(build_gcode, capsys):
    code = main(["check", str(build_gcode("clean")), "--printer", "prusa-mk4"])
    assert code == EXIT_CLEAN
    assert "no findings" in capsys.readouterr().out


def test_file_with_findings_exits_one(build_gcode, capsys):
    code = main(["check", str(build_gcode("hot", nozzle=250.0)), "--printer", "prusa-mk4"])
    assert code == EXIT_FINDINGS
    assert "temperature-range" in capsys.readouterr().out


def test_missing_file_exits_two(tmp_path, capsys):
    code = main(["check", str(tmp_path / "absent.gcode")])
    assert code == EXIT_UNPARSEABLE
    assert "gcode-lint:" in capsys.readouterr().err


def test_empty_file_exits_two(tmp_path, capsys):
    path = tmp_path / "empty.gcode"
    path.write_text("")
    assert main(["check", str(path)]) == EXIT_UNPARSEABLE
    assert "no gcode commands" in capsys.readouterr().err


def test_unknown_printer_exits_two(build_gcode, capsys):
    code = main(["check", str(build_gcode("p")), "--printer", "voron"])
    assert code == EXIT_UNPARSEABLE
    assert "unknown printer" in capsys.readouterr().err


def test_bad_bed_spec_exits_two(build_gcode, capsys):
    code = main(["check", str(build_gcode("p")), "--bed", "250x210"])
    assert code == EXIT_UNPARSEABLE
    assert "--bed" in capsys.readouterr().err


def test_bad_remaining_exits_two(build_gcode, capsys):
    code = main(["check", str(build_gcode("p")), "--remaining", "half a spool"])
    assert code == EXIT_UNPARSEABLE
    assert "bad mass" in capsys.readouterr().err


def test_json_output_parses(build_gcode, capsys):
    main(["check", str(build_gcode("j", nozzle=250.0)), "--printer", "prusa-mk4", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][0]["number"] == 3
    assert payload["fail_on"] == "medium"


def test_fail_on_high_ignores_medium(build_gcode, capsys):
    path = str(build_gcode("nohop", z_hop=False))
    assert main(["check", path, "--printer", "prusa-mk4", "--fail-on", "high"]) == EXIT_CLEAN
    assert main(["check", path, "--printer", "prusa-mk4", "--fail-on", "medium"]) == EXIT_FINDINGS
    capsys.readouterr()


def test_fail_on_low_catches_everything(build_gcode, capsys):
    path = str(build_gcode("nomat", declare_material=False))
    assert main(["check", path, "--printer", "prusa-mk4", "--fail-on", "medium"]) == EXIT_CLEAN
    assert main(["check", path, "--printer", "prusa-mk4", "--fail-on", "low"]) == EXIT_FINDINGS
    capsys.readouterr()


def test_bed_option_replaces_the_preset(build_gcode, capsys):
    path = str(build_gcode("wide", x_offset=140.0))
    assert main(["check", path, "--bed", "400x400x400"]) == EXIT_CLEAN
    assert main(["check", path, "--bed", "250x210x220"]) == EXIT_FINDINGS
    capsys.readouterr()


def test_printer_is_taken_from_the_header_when_not_given(build_gcode, capsys):
    main(["check", str(build_gcode("hdr", dialect="cura")), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["printer"]["key"] == "ender3"
    assert payload["printer"]["source"] == "header"


def test_remaining_flags_a_short_spool(build_gcode, capsys):
    code = main(["check", str(build_gcode("spool")), "--printer", "prusa-mk4",
                 "--remaining", "0.1g"])
    out = capsys.readouterr().out
    assert code == EXIT_FINDINGS
    assert "filament-remaining" in out
    assert "layer" in out


def test_stats_shows_a_layer_table(build_gcode, capsys):
    main(["check", str(build_gcode("s")), "--printer", "prusa-mk4", "--stats"])
    out = capsys.readouterr().out
    assert "max speed" in out and "mm/s" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_rules_subcommand_lists_all_twelve(capsys):
    assert main(["rules"]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "first-layer-speed" in out and "header-consistency" in out
    assert "bambu-x1c" in out


def test_no_arguments_prints_help(capsys):
    assert main([]) == EXIT_CLEAN
    assert "usage" in capsys.readouterr().out


def test_custom_first_layer_speed_threshold(build_gcode, capsys):
    path = str(build_gcode("fl", first_layer_speed=45.0))
    assert main(["check", path, "--printer", "prusa-mk4",
                 "--first-layer-speed", "50"]) == EXIT_CLEAN
    assert main(["check", path, "--printer", "prusa-mk4",
                 "--first-layer-speed", "30"]) == EXIT_FINDINGS
    capsys.readouterr()


@pytest.mark.parametrize(
    "text,grams",
    [("240g", 240.0), ("240", 240.0), ("1.2kg", 1200.0), ("  85.5 g ", 85.5), ("1KG", 1000.0)],
)
def test_mass_parsing(text, grams):
    assert parse_mass(text) == pytest.approx(grams)


@pytest.mark.parametrize("text", ["", "abc", "-5g", "0g", "12lb"])
def test_bad_mass_is_rejected(text):
    with pytest.raises(ValueError):
        parse_mass(text)
