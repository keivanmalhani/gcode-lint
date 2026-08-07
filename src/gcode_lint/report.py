"""Turning findings into something a person or a script can read."""

from __future__ import annotations

import json
import textwrap

from . import __version__
from .parser import ParseResult, format_duration
from .rules import (
    Finding,
    LintConfig,
    MATERIALS,
    SEVERITIES,
    at_or_above,
    estimated_grams,
    format_grams,
    grams_from_mm,
    material_of,
)

WIDTH = 88
_BODY_INDENT = " " * 7
_LABEL_WIDTH = 6


def counts(findings: list[Finding]) -> dict[str, int]:
    out = {name: 0 for name in SEVERITIES}
    for finding in findings:
        out[finding.severity] += 1
    return out


def exit_code(findings: list[Finding], fail_on: str) -> int:
    return 1 if any(at_or_above(f.severity, fail_on) for f in findings) else 0


def notes(state: ParseResult, cfg: LintConfig) -> list[str]:
    """Things that limited the analysis, said plainly."""
    out: list[str] = []
    if cfg.printer is None:
        out.append(
            "build volume was not checked; pass --printer NAME or --bed WxHxD to check it"
        )
    elif cfg.printer_source == "header":
        out.append(
            "printer taken from the slicer header (%s); pass --printer to override"
            % (state.header.printer_model or cfg.printer.name)
        )
    if state.travel_events_dropped:
        out.append(
            "travel evidence capped at %d moves, so the counts for rules 4 and 5 are "
            "lower bounds" % len(state.travel_events)
        )
    if state.inch_units:
        out.append("the file contains G20, so distances here may be inches, not mm")
    if not state.saw_layer_marker:
        out.append(
            "no slicer layer markers in the file; layers were inferred from z changes"
        )
    return out


def _wrap(label: str, text: str) -> list[str]:
    prefix = _BODY_INDENT + label.ljust(_LABEL_WIDTH)
    hanging = _BODY_INDENT + " " * _LABEL_WIDTH
    return textwrap.wrap(
        text, width=WIDTH, initial_indent=prefix, subsequent_indent=hanging
    ) or [prefix.rstrip()]


def _where(finding: Finding) -> str:
    if finding.count > 1 and finding.lines:
        shown = ", ".join(str(line) for line in finding.lines)
        extra = finding.count - len(finding.lines)
        if extra > 0 or finding.truncated:
            return "lines %s and %d more" % (shown, max(extra, 0))
        return "lines %s" % shown
    if finding.line:
        return "line %d" % finding.line
    return "whole file"


def format_finding(finding: Finding) -> str:
    head = "%-6s %-2d %-22s %s" % (
        finding.severity, finding.number, finding.rule, _where(finding)
    )
    lines = [head.rstrip()]
    lines.extend(
        textwrap.wrap(
            finding.title, width=WIDTH,
            initial_indent=_BODY_INDENT, subsequent_indent=_BODY_INDENT,
        )
    )
    lines.extend(_wrap("what", finding.detail))
    lines.extend(_wrap("why", finding.why))
    lines.extend(_wrap("fix", finding.fix))
    return "\n".join(lines)


def _summary_fields(state: ParseResult, cfg: LintConfig) -> list[tuple[str, str]]:
    header = state.header
    material = material_of(state)
    fields: list[tuple[str, str]] = [("slicer", header.describe_slicer())]

    if header.material:
        declared = header.material_raw or header.material
        known = "" if material else " (no print window on file for it)"
        fields.append(("material", "%s, declared as %s%s" % (header.material, declared, known)))
    else:
        fields.append(("material", "not declared in the header"))

    if cfg.printer:
        fields.append((
            "printer",
            "%s, %s mm, %s enclosure" % (
                cfg.printer.name, cfg.printer.volume, cfg.printer.enclosure
            ),
        ))
    elif header.printer_model:
        fields.append(("printer", "%s, no preset matched" % header.printer_model))
    else:
        fields.append(("printer", "not given"))

    first = state.first_layer
    fields.append((
        "layers",
        "%d, first layer z=%.2f mm" % (state.layer_count, first.z if first else 0.0),
    ))

    density = material.density if material else 1.24
    actual_g = grams_from_mm(state.totals.extrusion_mm, cfg.filament_diameter, density)
    filament = "file extrudes %.2f m (%s)" % (state.totals.extrusion_mm / 1000.0,
                                              format_grams(actual_g))
    declared = []
    if header.estimated_filament_mm:
        declared.append("%.2f m" % (header.estimated_filament_mm / 1000.0))
    if header.estimated_filament_g:
        declared.append(format_grams(header.estimated_filament_g))
    if declared:
        filament += "; header says %s" % " and ".join(declared)
    else:
        filament += "; header gives no total"
    fields.append(("filament", filament))

    time_text = "header says %s" % format_duration(header.estimated_time_s)
    time_text += "; moves in the file add up to %s with no acceleration model" % (
        format_duration(state.totals.time_s)
    )
    fields.append(("time", time_text))

    if state.x_max.seen and state.y_max.seen:
        fields.append((
            "extent",
            "X %.1f to %.1f, Y %.1f to %.1f, Z up to %.2f mm" % (
                state.x_min.value, state.x_max.value,
                state.y_min.value, state.y_max.value, state.z_max.value,
            ),
        ))
    if cfg.remaining_g is not None:
        needed, source = estimated_grams(state, cfg)
        fields.append((
            "spool",
            "%s left, this file needs about %s (%s)" % (
                format_grams(cfg.remaining_g), format_grams(needed), source
            ),
        ))
    return fields


def render_text(
    state: ParseResult, findings: list[Finding], cfg: LintConfig, stats: bool = False
) -> str:
    out: list[str] = ["gcode-lint %s  %s" % (__version__, state.source), ""]
    for label, value in _summary_fields(state, cfg):
        out.extend(
            textwrap.wrap(
                value, width=WIDTH,
                initial_indent="%-10s" % label, subsequent_indent=" " * 10,
            )
        )
    out.append("")

    if findings:
        for finding in findings:
            out.append(format_finding(finding))
            out.append("")
    else:
        out.append("no findings")
        out.append("")

    if stats:
        out.extend(render_stats(state))
        out.append("")

    for note in notes(state, cfg):
        out.extend(
            textwrap.wrap(
                note, width=WIDTH, initial_indent="note      ", subsequent_indent=" " * 10
            )
        )

    tally = counts(findings)
    if findings:
        out.append(
            "%d high, %d medium, %d low in %d lines"
            % (tally["high"], tally["medium"], tally["low"], state.line_count)
        )
    else:
        out.append("clean, %d lines checked" % state.line_count)
    return "\n".join(out) + "\n"


def render_stats(state: ParseResult) -> list[str]:
    rows = ["layer      z       time     filament    max speed   moves"]
    rows.append("-" * 58)
    if state.start_gcode is not None:
        start = state.start_gcode
        rows.append(
            "%-6s %6s %9s %9.1f mm %6.1f mm/s %6d"
            % ("start", "-", format_duration(start.time_s), start.extrusion_mm,
               start.max_speed, start.moves)
        )
    for layer in state.layers:
        rows.append(
            "%-6d %6.2f %9s %9.1f mm %6.1f mm/s %6d"
            % (layer.index, layer.z, format_duration(layer.time_s),
               layer.extrusion_mm, layer.max_speed, layer.moves)
        )
    rows.append("-" * 58)
    rows.append(
        "%-6s %6s %9s %9.1f mm %6s     %6d"
        % ("total", "", format_duration(state.totals.time_s),
           state.totals.extrusion_mm, "", state.totals.moves)
    )
    return rows


def build_payload(
    state: ParseResult,
    findings: list[Finding],
    cfg: LintConfig,
    stats: bool = False,
    fail_on: str = "medium",
) -> dict:
    header = state.header
    material = material_of(state)
    density = material.density if material else 1.24
    payload: dict = {
        "tool": "gcode-lint",
        "version": __version__,
        "file": state.source,
        "slicer": {"name": header.slicer, "version": header.slicer_version},
        "material": {
            "code": header.material,
            "declared": header.material_raw,
            "known": header.material in MATERIALS if header.material else False,
        },
        "printer": None,
        "summary": {
            "lines": state.line_count,
            "layers": state.layer_count,
            "first_layer_z": round(state.first_layer.z, 3) if state.first_layer else None,
            "extrusion_mm": round(state.totals.extrusion_mm, 2),
            "extrusion_g": round(
                grams_from_mm(state.totals.extrusion_mm, cfg.filament_diameter, density), 2
            ),
            "print_mm": round(state.totals.print_mm, 2),
            "travel_mm": round(state.totals.travel_mm, 2),
            "header_time_s": header.estimated_time_s,
            "moves_time_s": round(state.totals.time_s, 1),
            "header_filament_mm": header.estimated_filament_mm,
            "header_filament_g": header.estimated_filament_g,
            "bounds": {
                "x": [round(state.x_min.value, 3), round(state.x_max.value, 3)],
                "y": [round(state.y_min.value, 3), round(state.y_max.value, 3)],
                "z": [0.0, round(state.z_max.value, 3)],
            },
            "counts": counts(findings),
        },
        "notes": notes(state, cfg),
        "findings": [finding.to_dict() for finding in findings],
        "fail_on": fail_on,
        "exit_code": exit_code(findings, fail_on),
    }
    if cfg.printer is not None:
        payload["printer"] = {
            "key": cfg.printer.key,
            "name": cfg.printer.name,
            "x": cfg.printer.x,
            "y": cfg.printer.y,
            "z": cfg.printer.z,
            "enclosure": cfg.printer.enclosure,
            "source": cfg.printer_source,
        }
    if cfg.remaining_g is not None:
        needed, source = estimated_grams(state, cfg)
        payload["summary"]["remaining_g"] = cfg.remaining_g
        payload["summary"]["needed_g"] = round(needed, 2)
        payload["summary"]["needed_from"] = source
    if stats:
        payload["layers"] = [
            {
                "index": layer.index,
                "z": round(layer.z, 3),
                "start_line": layer.start_line,
                "end_line": layer.end_line,
                "time_s": round(layer.time_s, 2),
                "extrusion_mm": round(layer.extrusion_mm, 3),
                "print_mm": round(layer.print_mm, 2),
                "travel_mm": round(layer.travel_mm, 2),
                "max_speed_mm_s": round(layer.max_speed, 2),
                "moves": layer.moves,
            }
            for layer in state.layers
        ]
    return payload


def render_json(
    state: ParseResult,
    findings: list[Finding],
    cfg: LintConfig,
    stats: bool = False,
    fail_on: str = "medium",
) -> str:
    return json.dumps(build_payload(state, findings, cfg, stats, fail_on), indent=2) + "\n"
