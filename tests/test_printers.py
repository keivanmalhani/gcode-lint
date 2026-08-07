"""Printer presets, bed specifications and header matching."""

from __future__ import annotations

import pytest

from gcode_lint.printers import (
    ENCLOSURE_KINDS,
    PRINTERS,
    PrinterError,
    match_model,
    parse_bed,
    preset_table,
    resolve_printer,
)


def test_all_five_presets_ship():
    assert set(PRINTERS) == {"bambu-a1", "bambu-p1s", "bambu-x1c", "prusa-mk4", "ender3"}


@pytest.mark.parametrize("key", sorted(PRINTERS))
def test_presets_have_a_sane_volume(key):
    printer = PRINTERS[key]
    assert printer.x > 0 and printer.y > 0 and printer.z > 0
    assert printer.enclosure in ENCLOSURE_KINDS


@pytest.mark.parametrize(
    "name,expected",
    [
        ("bambu-a1", "bambu-a1"),
        ("A1", "bambu-a1"),
        ("x1c", "bambu-x1c"),
        ("X1 Carbon", "bambu-x1c"),
        ("mk4", "prusa-mk4"),
        ("Prusa MK4", "prusa-mk4"),
        ("ender 3", "ender3"),
        ("Ender-3", "ender3"),
    ],
)
def test_aliases_resolve(name, expected):
    assert resolve_printer(name).key == expected


def test_unknown_printer_lists_the_presets():
    with pytest.raises(PrinterError) as excinfo:
        resolve_printer("voron")
    assert "bambu-a1" in str(excinfo.value)


def test_bed_specification():
    printer = parse_bed("250x210x220")
    assert (printer.x, printer.y, printer.z) == (250.0, 210.0, 220.0)
    assert printer.key == "custom"


def test_bed_specification_accepts_decimals_and_capitals():
    printer = parse_bed("256.5X256.5X256")
    assert printer.x == pytest.approx(256.5)


@pytest.mark.parametrize("spec", ["250x210", "abc", "250x210x220x1", "0x10x10", "-1x2x3"])
def test_bad_bed_specifications_are_rejected(spec):
    with pytest.raises(PrinterError):
        parse_bed(spec)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("MK4", "prusa-mk4"),
        ("Bambu Lab X1 Carbon", "bambu-x1c"),
        ("Bambu Lab P1S", "bambu-p1s"),
        ("Creality Ender-3", "ender3"),
        ("Original Prusa MK4 Input Shaper", "prusa-mk4"),
    ],
)
def test_header_models_match_presets(model, expected):
    matched = match_model(model)
    assert matched is not None and matched.key == expected


@pytest.mark.parametrize("model", [None, "", "Voron 2.4 350", "Some Machine"])
def test_unmatched_header_models_return_none(model):
    assert match_model(model) is None


def test_over_reports_the_overrun():
    printer = resolve_printer("prusa-mk4")
    assert printer.over("x", 260.0) == pytest.approx(10.0)
    assert printer.over("y", 100.0) == 0.0


def test_preset_table_is_renderable():
    rows = preset_table()
    assert len(rows) == len(PRINTERS)
    assert all(len(row) == 4 for row in rows)


def test_describe_mentions_volume_and_enclosure():
    text = resolve_printer("bambu-p1s").describe()
    assert "256x256x256" in text and "passive" in text
