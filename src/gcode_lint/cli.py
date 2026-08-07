"""Command line entry point."""

from __future__ import annotations

import argparse
import re
import sys

from . import __version__
from .parser import ParseError, parse_file
from .printers import PrinterError, match_model, parse_bed, preset_table, resolve_printer
from .report import exit_code, render_json, render_text
from .rules import LintConfig, RULE_INFO, SEVERITIES, run_rules

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_UNPARSEABLE = 2

_MASS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(kg|g)?\s*$", re.IGNORECASE)


def parse_mass(text: str) -> float:
    """Read 240g, 240, or 1.2kg as grams."""
    match = _MASS_RE.match(text)
    if not match:
        raise ValueError("bad mass %r; expected something like 240g or 1.2kg" % text)
    value = float(match.group(1))
    if (match.group(2) or "g").lower() == "kg":
        value *= 1000.0
    if value <= 0:
        raise ValueError("bad mass %r; must be greater than zero" % text)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcode-lint",
        description=(
            "Static analysis for sliced 3D printer gcode. Reads the file once and "
            "reports what is going to go wrong before you start the print."
        ),
    )
    parser.add_argument("--version", action="version", version="gcode-lint %s" % __version__)
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="lint one gcode file")
    check.add_argument("file", help="path to a sliced .gcode file")
    check.add_argument(
        "--printer",
        metavar="NAME",
        help="printer preset: %s" % ", ".join(key for key, _, _, _ in preset_table()),
    )
    check.add_argument(
        "--bed", metavar="WxHxD", help="build volume in mm, for example 250x210x220"
    )
    check.add_argument(
        "--remaining", metavar="MASS", help="filament left on the spool, for example 240g"
    )
    check.add_argument("--json", action="store_true", help="machine readable output")
    check.add_argument(
        "--fail-on",
        choices=SEVERITIES,
        default="medium",
        help="lowest severity that exits 1 (default: medium)",
    )
    check.add_argument(
        "--stats", action="store_true", help="layer by layer time, extrusion and max speed"
    )
    check.add_argument(
        "--first-layer-speed",
        type=float,
        default=40.0,
        metavar="MM_S",
        help="first layer speed threshold in mm/s (default: 40)",
    )
    check.add_argument(
        "--travel-threshold",
        type=float,
        default=3.0,
        metavar="MM",
        help="travel length that should be retracted, in mm (default: 3)",
    )
    check.add_argument(
        "--diameter",
        type=float,
        default=1.75,
        metavar="MM",
        help="filament diameter in mm (default: 1.75)",
    )

    subparsers.add_parser("rules", help="list the checks")
    return parser


def _resolve_printer(args, header_model):
    """Pick the build volume: --bed wins, then --printer, then the header."""
    if args.bed:
        return parse_bed(args.bed), "--bed"
    if args.printer:
        return resolve_printer(args.printer), "--printer"
    matched = match_model(header_model)
    if matched is not None:
        return matched, "header"
    return None, "none"


def _print_rules(stream) -> None:
    stream.write("gcode-lint %s checks:\n\n" % __version__)
    for number, name, description in RULE_INFO:
        stream.write("%2d  %-20s %s\n" % (number, name, description))
    stream.write("\nprinter presets:\n\n")
    for key, name, volume, enclosure in preset_table():
        stream.write("    %-12s %-22s %-16s %s enclosure\n" % (key, name, volume, enclosure))
    stream.write("    %-12s %-22s %s\n" % ("--bed WxHxD", "anything else", "in mm"))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stdout, stderr = sys.stdout, sys.stderr

    if args.command == "rules":
        _print_rules(stdout)
        return EXIT_CLEAN
    if args.command != "check":
        parser.print_help(stdout)
        return EXIT_CLEAN

    remaining = None
    if args.remaining:
        try:
            remaining = parse_mass(args.remaining)
        except ValueError as exc:
            stderr.write("gcode-lint: %s\n" % exc)
            return EXIT_UNPARSEABLE

    try:
        state = parse_file(args.file)
    except ParseError as exc:
        stderr.write("gcode-lint: %s\n" % exc)
        return EXIT_UNPARSEABLE

    try:
        printer, source = _resolve_printer(args, state.header.printer_model)
    except PrinterError as exc:
        stderr.write("gcode-lint: %s\n" % exc)
        return EXIT_UNPARSEABLE

    config = LintConfig(
        printer=printer,
        printer_source=source,
        remaining_g=remaining,
        first_layer_speed_max=args.first_layer_speed,
        travel_retract_mm=args.travel_threshold,
        filament_diameter=args.diameter,
    )
    findings = run_rules(state, config)

    if args.json:
        stdout.write(render_json(state, findings, config, args.stats, args.fail_on))
    else:
        stdout.write(render_text(state, findings, config, args.stats))
    return exit_code(findings, args.fail_on)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
