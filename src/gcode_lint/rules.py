"""The checks.

Every rule is a plain function of (ParseResult, LintConfig) that returns a
list of Findings. Rules never read the file and never talk to the terminal,
so a test can hand one a hand built ParseResult and assert on the result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .parser import ParseResult, format_duration
from .printers import Printer

SEVERITIES = ("high", "medium", "low")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}


def severity_rank(name: str) -> int:
    return _SEVERITY_RANK[name]


def at_or_above(severity: str, floor: str) -> bool:
    return _SEVERITY_RANK[severity] <= _SEVERITY_RANK[floor]


@dataclass(frozen=True)
class Finding:
    """One thing that is going to go wrong."""

    rule: str
    number: int
    severity: str
    title: str
    detail: str
    why: str
    fix: str
    line: int | None = None
    count: int = 1
    lines: tuple[int, ...] = ()
    truncated: bool = False
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "number": self.number,
            "severity": self.severity,
            "title": self.title,
            "line": self.line,
            "detail": self.detail,
            "why": self.why,
            "fix": self.fix,
            "count": self.count,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "data": self.data,
        }


@dataclass(frozen=True)
class Material:
    """Print window and physical constants for one material."""

    name: str
    nozzle_min: float
    nozzle_max: float
    bed_min: float
    bed_max: float
    density: float          # g/cm3
    first_layer_fan: bool   # is part cooling acceptable on layer one
    wants_chamber: bool
    chamber_c: float = 0.0


MATERIALS: dict[str, Material] = {
    "PLA":  Material("PLA", 190, 235, 45, 70, 1.24, True, False),
    "PETG": Material("PETG", 220, 260, 65, 90, 1.27, True, False),
    "ABS":  Material("ABS", 230, 275, 90, 110, 1.04, False, True, 50),
    "ASA":  Material("ASA", 240, 280, 90, 110, 1.07, False, True, 50),
    "TPU":  Material("TPU", 200, 240, 30, 60, 1.21, True, False),
    "PC":   Material("PC", 250, 310, 90, 120, 1.20, False, True, 60),
    "PA":   Material("PA", 240, 300, 60, 110, 1.14, False, True, 50),
    "PVA":  Material("PVA", 180, 215, 45, 65, 1.23, True, False),
    "PVB":  Material("PVB", 200, 230, 60, 80, 1.08, True, False),
    "HIPS": Material("HIPS", 220, 250, 90, 110, 1.04, False, True, 45),
    "PP":   Material("PP", 220, 260, 80, 110, 0.90, True, False),
    "PCTG": Material("PCTG", 240, 270, 70, 90, 1.23, True, False),
    "PPS":  Material("PPS", 300, 340, 110, 130, 1.35, False, True, 70),
    "PEEK": Material("PEEK", 380, 430, 120, 145, 1.30, False, True, 90),
    "PEI":  Material("PEI", 350, 390, 110, 140, 1.27, False, True, 80),
}

DEFAULT_DENSITY = 1.24


@dataclass
class LintConfig:
    """Thresholds and the things the user told us on the command line."""

    printer: Printer | None = None
    printer_source: str = "none"
    remaining_g: float | None = None
    first_layer_speed_max: float = 40.0
    travel_retract_mm: float = 3.0
    travel_zhop_mm: float = 5.0
    filament_diameter: float = 1.75
    header_mismatch_pct: float = 10.0
    min_extrude_temp: float = 150.0
    purge_extrusion_mm: float = 5.0
    max_lines_per_finding: int = 6


def grams_from_mm(mm: float, diameter: float, density: float) -> float:
    """Filament length in mm to mass in grams."""
    area_mm2 = math.pi * (diameter / 2.0) ** 2
    return area_mm2 * mm / 1000.0 * density


def format_grams(grams: float) -> str:
    """One decimal below ten grams, none above, because nobody weighs a
    400 g spool to a tenth of a gram."""
    return ("%.1f g" if grams < 10 else "%.0f g") % grams


def material_of(state: ParseResult) -> Material | None:
    code = state.header.material
    return MATERIALS.get(code) if code else None


def _lines_of(events, cfg: LintConfig) -> tuple[tuple[int, ...], bool]:
    lines = tuple(event.line for event in events[: cfg.max_lines_per_finding])
    return lines, len(events) > cfg.max_lines_per_finding


# -- 1 ---------------------------------------------------------------------

def rule_first_layer_speed(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    layer = state.first_layer
    if layer is None or layer.max_speed <= 0:
        return []
    limit = cfg.first_layer_speed_max
    if layer.max_speed <= limit:
        return []
    severity = "high" if layer.max_speed > limit * 1.5 else "medium"
    return [
        Finding(
            rule="first-layer-speed",
            number=1,
            severity=severity,
            title="first layer runs faster than the threshold",
            line=layer.max_speed_line,
            detail=(
                "the first layer extrudes at up to %.1f mm/s, above the %.1f mm/s "
                "threshold (layer 1, z=%.2f mm)"
                % (layer.max_speed, limit, layer.z)
            ),
            why=(
                "the first layer is the only one bonded to a plate rather than to "
                "more plastic. Laid down quickly it has less time to flatten against "
                "the bed and less time to stay molten, so it grips along a narrower "
                "footprint. That is where corner lift twelve hours later starts."
            ),
            fix=(
                "set the first layer speed to %.0f mm/s or lower (PrusaSlicer: Speed, "
                "First layer speed; Cura: Initial Layer Speed; Orca and Bambu Studio: "
                "Initial layer speed)."
                % min(limit, 30.0)
            ),
            data={"max_speed": round(layer.max_speed, 2), "threshold": limit},
        )
    ]


# -- 2 ---------------------------------------------------------------------

def rule_bed_adhesion(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    if state.totals.print_mm <= 0:
        return []
    if state.adhesion_feature:
        return []
    if state.start_gcode_extrusion_mm >= cfg.purge_extrusion_mm:
        return []
    if state.first_object_feature and state.pre_object_extrusion_mm >= cfg.purge_extrusion_mm:
        return []
    if state.first_object_feature:
        where = "the first feature in the file is %s at line %d" % (
            state.first_object_feature,
            state.first_object_feature_line,
        )
    else:
        where = "the first extrusion is at line %d" % state.first_extrusion_line
    return [
        Finding(
            rule="bed-adhesion",
            number=2,
            severity="medium",
            title="nothing is printed before the model",
            line=state.first_object_feature_line or state.first_extrusion_line,
            detail=(
                "no skirt, brim, raft or purge line runs before the model starts; %s, "
                "and only %.1f mm of filament is extruded before it"
                % (where, max(state.pre_object_extrusion_mm, state.start_gcode_extrusion_mm))
            ),
            why=(
                "the model's first extrusion comes out of a nozzle holding an unknown "
                "amount of plastic and with no pressure established, so the first few "
                "centimetres of the outline are thin or missing. A skirt also shows you "
                "the first layer height while there is still time to stop the print."
            ),
            fix=(
                "turn on a skirt of at least one loop, or a brim if the footprint is "
                "small, or add a purge line to the start gcode."
            ),
            data={"pre_object_extrusion_mm": round(state.pre_object_extrusion_mm, 2)},
        )
    ]


# -- 3 ---------------------------------------------------------------------

def _temp_code(state: ParseResult, line: int, default: str) -> str:
    for event in state.temp_events:
        if event.line == line:
            return event.code
    return default


def rule_temperature_range(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    material = material_of(state)
    if material is None:
        if not state.nozzle_max.seen:
            return []
        return [
            Finding(
                rule="temperature-range",
                number=3,
                severity="low",
                title="material not declared, temperature not checked",
                line=None,
                detail=(
                    "no filament type is declared in the header, so the hotend target "
                    "of %.0f C could not be checked against a material window"
                    % state.nozzle_max.value
                ),
                why=(
                    "every temperature check depends on knowing what is in the "
                    "extruder. Without it this file gets no temperature checking at all."
                ),
                fix=(
                    "re-export from the slicer with the filament profile selected, or "
                    "add a '; filament_type = PLA' comment if this file was hand built."
                ),
                data={},
            )
        ]

    findings: list[Finding] = []
    if state.nozzle_max.seen and state.nozzle_max.value > material.nozzle_max:
        code = _temp_code(state, state.nozzle_max.line, "M104")
        findings.append(
            Finding(
                rule="temperature-range",
                number=3,
                severity="high",
                title="hotend hotter than the material tolerates",
                line=state.nozzle_max.line,
                detail=(
                    "%s sets the hotend to %.0f C; %s prints between %.0f and %.0f C"
                    % (code, state.nozzle_max.value, material.name,
                       material.nozzle_min, material.nozzle_max)
                ),
                why=(
                    "past the top of its window the polymer starts breaking down in the "
                    "melt zone rather than just melting. It goes thin and oozes between "
                    "islands, and the residue bakes onto the nozzle and drops back onto "
                    "the part as specks."
                ),
                fix=(
                    "set the nozzle to %.0f-%.0f C for %s and re-slice."
                    % (material.nozzle_min + 10, material.nozzle_max - 10, material.name)
                ),
                data={"target": state.nozzle_max.value, "material": material.name,
                      "window": [material.nozzle_min, material.nozzle_max]},
            )
        )
    elif state.nozzle_min.seen and state.nozzle_min.value < material.nozzle_min:
        code = _temp_code(state, state.nozzle_min.line, "M104")
        findings.append(
            Finding(
                rule="temperature-range",
                number=3,
                severity="high",
                title="hotend colder than the material needs",
                line=state.nozzle_min.line,
                detail=(
                    "%s sets the hotend to %.0f C; %s prints between %.0f and %.0f C"
                    % (code, state.nozzle_min.value, material.name,
                       material.nozzle_min, material.nozzle_max)
                ),
                why=(
                    "below its window the plastic is soft but not properly molten, so "
                    "layers sit on each other instead of welding. The part looks correct "
                    "and then splits along a layer line the first time it is loaded."
                ),
                fix=(
                    "set the nozzle to %.0f-%.0f C for %s and re-slice."
                    % (material.nozzle_min + 10, material.nozzle_max - 10, material.name)
                ),
                data={"target": state.nozzle_min.value, "material": material.name,
                      "window": [material.nozzle_min, material.nozzle_max]},
            )
        )

    if state.bed_max.seen and state.bed_max.value > material.bed_max:
        findings.append(_bed_finding(state.bed_max, material, "hotter", cfg))
    elif state.bed_min.seen and state.bed_min.value < material.bed_min:
        findings.append(_bed_finding(state.bed_min, material, "colder", cfg))
    return findings


def _bed_finding(extreme, material: Material, direction: str, cfg: LintConfig) -> Finding:
    if direction == "hotter":
        why = (
            "an over hot plate keeps the bottom few layers soft long after they are "
            "printed. The part sags into the sheet, the first layer spreads, and small "
            "features on the underside close up."
        )
    else:
        why = (
            "a plate below the material's range lets the first layer cool and contract "
            "while it is still being printed, which is what pulls the corners up."
        )
    return Finding(
        rule="temperature-range",
        number=3,
        severity="medium",
        title="bed %s than the material wants" % direction,
        line=extreme.line,
        detail=(
            "the bed is set to %.0f C; %s wants %.0f to %.0f C"
            % (extreme.value, material.name, material.bed_min, material.bed_max)
        ),
        why=why,
        fix="set the bed to %.0f-%.0f C for %s." % (
            material.bed_min, material.bed_max, material.name
        ),
        data={"target": extreme.value, "material": material.name,
              "window": [material.bed_min, material.bed_max]},
    )


# -- 4 ---------------------------------------------------------------------

def rule_travel_retraction(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    if state.first_extrusion_line == 0:
        return []
    events = [
        event
        for event in state.travel_events
        if not event.retracted
        and event.length >= cfg.travel_retract_mm
        and event.line > state.first_extrusion_line
    ]
    if not events:
        return []
    longest = max(event.length for event in events)
    severity = "high" if len(events) >= 10 or longest >= 50.0 else "medium"
    lines, truncated = _lines_of(events, cfg)
    return [
        Finding(
            rule="travel-retraction",
            number=4,
            severity=severity,
            title="long travels with no retraction",
            line=events[0].line,
            detail=(
                "%d travel move%s longer than %.1f mm start with the filament still "
                "under pressure; the longest is %.1f mm"
                % (len(events), "" if len(events) == 1 else "s",
                   cfg.travel_retract_mm, longest)
            ),
            why=(
                "pressure left in the melt zone keeps pushing plastic out while the "
                "head crosses open air, and it lands as a thread between the two ends "
                "of the travel. Stringing is not a travel path problem, it is a nozzle "
                "that was never depressurised."
            ),
            fix=(
                "enable retraction and lower the travel distance that triggers it below "
                "%.1f mm (PrusaSlicer: Retraction, Minimum travel after retraction; "
                "Cura: Retraction Minimum Travel; Orca and Bambu Studio: Retraction, "
                "Travel distance threshold)."
                % cfg.travel_retract_mm
            ),
            count=len(events),
            lines=lines,
            truncated=truncated or state.travel_events_dropped > 0,
            data={"longest_mm": round(longest, 2), "threshold_mm": cfg.travel_retract_mm},
        )
    ]


# -- 5 ---------------------------------------------------------------------

def rule_z_hop(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    events = [
        event
        for event in state.travel_events
        if event.crossed_print and not event.z_hop and event.length >= cfg.travel_zhop_mm
    ]
    if not events:
        return []
    severity = "high" if len(events) >= 25 else "medium"
    lines, truncated = _lines_of(events, cfg)
    return [
        Finding(
            rule="z-hop",
            number=5,
            severity=severity,
            title="travel crosses printed material at layer height",
            line=events[0].line,
            detail=(
                "%d travel move%s longer than %.1f mm cross material already laid down "
                "on the same layer without lifting z"
                % (len(events), "" if len(events) == 1 else "s", cfg.travel_zhop_mm)
            ),
            why=(
                "the nozzle sits at exactly the height of the material it just laid "
                "down, so crossing it drags the hot tip through the top surface. That "
                "leaves a scar, and on a tall thin wall or a small part it is enough to "
                "push the part off the plate."
            ),
            fix=(
                "turn on z hop of 0.2 to 0.4 mm (PrusaSlicer: Lift Z; Cura: Z Hop When "
                "Retracted; Orca and Bambu Studio: Z hop height), or set travel to avoid "
                "crossing perimeters."
            ),
            count=len(events),
            lines=lines,
            truncated=truncated or state.travel_events_dropped > 0,
            data={"threshold_mm": cfg.travel_zhop_mm},
        )
    ]


# -- 6 ---------------------------------------------------------------------

def rule_first_layer_fan(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    material = material_of(state)
    if material is None or material.first_layer_fan:
        return []
    fan = state.first_layer_fan
    if not fan.seen or fan.value <= 0:
        return []
    severity = "high" if fan.value >= 50 else "medium"
    return [
        Finding(
            rule="first-layer-fan",
            number=6,
            severity=severity,
            title="part cooling fan runs on the first layer",
            line=fan.line,
            detail=(
                "the part cooling fan reaches %.0f%% during the first layer, and %s "
                "needs it off there" % (fan.value, material.name)
            ),
            why=(
                "%s contracts measurably as it cools. Blowing air on the first layer "
                "makes it shrink while the layers above are still going down hot and "
                "expanded, and the difference pulls the corners off the plate. Warping "
                "on this material is a cooling problem before it is an adhesion problem."
                % material.name
            ),
            fix=(
                "set first layer fan speed to 0 for %s and keep the fan low for the "
                "first several layers." % material.name
            ),
            data={"percent": round(fan.value, 1), "material": material.name},
        )
    ]


# -- 7 ---------------------------------------------------------------------

def rule_cold_extrusion(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    events = state.cold_extrude_events
    if not events:
        return []
    first = events[0]
    lines, truncated = _lines_of(events, cfg)
    count = len(events) + state.cold_extrude_events_dropped
    if first.target is None or first.target < cfg.min_extrude_temp:
        target = "no hotend temperature has been commanded at all" if first.target is None \
            else "the hotend target is only %.0f C" % first.target
        return [
            Finding(
                rule="cold-extrusion",
                number=7,
                severity="high",
                title="extrusion before the hotend is hot",
                line=first.line,
                detail="the first extrusion is at line %d and %s" % (first.line, target),
                why=(
                    "firmware refuses to turn the extruder below its cold extrusion "
                    "limit, so the print starts with no plastic and runs the whole file "
                    "dry. With the limit disabled the drive gear instead grinds a flat "
                    "spot into the filament and the extruder jams."
                ),
                fix=(
                    "wait for temperature with M109 S<temp> before the first extrusion. "
                    "Slicers emit this by default, so a hand edited start gcode is the "
                    "usual cause."
                ),
                count=count,
                lines=lines,
                truncated=truncated or state.cold_extrude_events_dropped > 0,
                data={"target": first.target},
            )
        ]
    return [
        Finding(
            rule="cold-extrusion",
            number=7,
            severity="medium",
            title="extrusion starts without waiting for temperature",
            line=first.line,
            detail=(
                "%d extrusion move%s run before any M109 wait; the hotend was asked for "
                "%.0f C with M104, which does not block"
                % (count, "" if count == 1 else "s", first.target)
            ),
            why=(
                "M104 sets a target and returns immediately, so the printer starts "
                "laying plastic while the hotend is still climbing. The first "
                "centimetres come out short and cold, which is exactly where the skirt "
                "or the first perimeter is."
            ),
            fix="replace the M104 before the first extrusion with M109 S<temp>.",
            count=count,
            lines=lines,
            truncated=truncated or state.cold_extrude_events_dropped > 0,
            data={"target": first.target},
        )
    ]


# -- 8 ---------------------------------------------------------------------

def rule_build_volume(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    printer = cfg.printer
    if printer is None:
        return []
    findings: list[Finding] = []
    checks = (
        ("X", state.x_max, printer.x, state.x_min),
        ("Y", state.y_max, printer.y, state.y_min),
        ("Z", state.z_max, printer.z, None),
    )
    for axis, high, limit, low in checks:
        if high.seen and high.value > limit + 1e-6:
            findings.append(_volume_finding(state, axis, high, limit, printer, "above"))
        if low is not None and low.seen and low.value < -1e-6:
            findings.append(_volume_finding(state, axis, low, 0.0, printer, "below"))
    return findings


def _volume_finding(state, axis, extreme, limit, printer, direction) -> Finding:
    after_print = (
        state.end.last_extrusion_line > 0 and extreme.line > state.end.last_extrusion_line
    )
    over = abs(extreme.value - limit)
    if direction == "above":
        detail = "%s reaches %.1f mm, %.1f mm past the %s limit of %.0f mm" % (
            axis, extreme.value, over, printer.name, limit,
        )
    else:
        detail = "%s reaches %.1f mm, %.1f mm below the origin on %s" % (
            axis, extreme.value, over, printer.name,
        )
    if after_print:
        detail += " (in the end gcode, after the last extrusion)"
        why = (
            "the move is a park or a lift rather than part of the model, so nothing "
            "prints out of bounds, but the printer still drives the axis into its hard "
            "stop at the end of every print and grinds there until the file ends."
        )
        fix = "trim the park move in the end gcode to stay inside the build volume."
        severity = "low"
    else:
        why = (
            "the printer does not stop at the edge of its own travel. It drives the "
            "axis into the end of the rail and keeps commanding motion, which either "
            "skips steps and shifts every layer above that point, or stalls against the "
            "frame for the rest of the print."
        )
        fix = (
            "move the model back onto the plate in the slicer and re-slice, or lint "
            "with the preset for the machine this file was actually sliced for."
        )
        severity = "high"
    return Finding(
        rule="build-volume",
        number=8,
        severity=severity,
        title="%s axis leaves the build volume" % axis.lower(),
        line=extreme.line,
        detail=detail,
        why=why,
        fix=fix,
        data={"axis": axis, "value": round(extreme.value, 3), "limit": limit,
              "printer": printer.name},
    )


# -- 9 ---------------------------------------------------------------------

def rule_end_retraction(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    end = state.end
    if end.last_extrusion_line == 0:
        return []
    if end.retracted_at_end:
        if (
            end.first_move_after_print_line
            and end.last_retract_line > end.first_move_after_print_line
        ):
            return [
                Finding(
                    rule="end-retraction",
                    number=9,
                    severity="low",
                    title="end retraction happens after the head moves away",
                    line=end.last_retract_line,
                    detail=(
                        "the toolhead leaves the part at line %d and only retracts at "
                        "line %d" % (end.first_move_after_print_line, end.last_retract_line)
                    ),
                    why=(
                        "the nozzle is still pressurised for the whole park move, so it "
                        "drags a thread from the last printed point to wherever it parks."
                    ),
                    fix="retract before the park move in the end gcode, not after it.",
                    data={},
                )
            ]
        return []
    return [
        Finding(
            rule="end-retraction",
            number=9,
            severity="medium",
            title="end gcode never retracts",
            line=end.first_move_after_print_line or end.last_extrusion_line,
            detail=(
                "the last extrusion is at line %d and nothing retracts the filament "
                "before the file ends" % end.last_extrusion_line
            ),
            why=(
                "the nozzle parks and the bed cools with the melt zone still under "
                "pressure. Whatever is in there runs out over the following minutes, "
                "usually onto the top surface of the part, and welds itself on as it "
                "cools."
            ),
            fix=(
                "add a retraction such as G1 E-2 F2400 to the end gcode before the "
                "park move."
            ),
            data={},
        )
    ]


# -- 10 --------------------------------------------------------------------

def rule_chamber(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    material = material_of(state)
    if material is None or not material.wants_chamber:
        return []
    if state.chamber_max.seen and state.chamber_max.value > 0:
        return []
    if state.header.chamber_temp and state.header.chamber_temp > 0:
        return []
    enclosure = cfg.printer.enclosure if cfg.printer else "unknown"
    if enclosure == "passive":
        severity = "low"
        fix = (
            "%s has no chamber heater, so close the doors and the top, let it soak for "
            "ten minutes before starting, and keep the part cooling fan low."
            % cfg.printer.name
        )
    elif enclosure == "active":
        severity = "medium"
        fix = "set the chamber with M141 S%.0f in the start gcode." % material.chamber_c
    else:
        severity = "medium"
        fix = (
            "print this on an enclosed machine, or enclose this one and let it reach "
            "about %.0f C before starting. With an active heater, M141 S%.0f in the "
            "start gcode does it." % (material.chamber_c, material.chamber_c)
        )
    return [
        Finding(
            rule="chamber",
            number=10,
            severity=severity,
            title="no chamber temperature for a material that wants one",
            line=None,
            detail=(
                "%s is being printed with no chamber temperature declared: no M141 or "
                "M191 in the file and no chamber_temperature in the header"
                % material.name
            ),
            why=(
                "%s contracts as it cools. In open air the lower layers cool and shrink "
                "while the upper layers are still going down hot, and that gradient "
                "either lifts the part off the plate or splits it along a layer line "
                "part way up. A warm still chamber around %.0f C removes the gradient."
                % (material.name, material.chamber_c)
            ),
            fix=fix,
            data={"material": material.name, "wants_c": material.chamber_c,
                  "enclosure": enclosure},
        )
    ]


# -- 11 --------------------------------------------------------------------

def estimated_grams(state: ParseResult, cfg: LintConfig) -> tuple[float, str]:
    """Grams of filament this file needs, and where the number came from.

    The slicer header is preferred because it accounts for things the raw
    extrusion total does not, but a header that disagrees with the file by
    more than the rule 12 threshold is stale and gets ignored.
    """
    material = material_of(state)
    density = material.density if material else DEFAULT_DENSITY
    actual = grams_from_mm(state.totals.extrusion_mm, cfg.filament_diameter, density)

    declared: float | None = state.header.estimated_filament_g
    if declared is None and state.header.estimated_filament_mm:
        declared = grams_from_mm(
            state.header.estimated_filament_mm, cfg.filament_diameter, density
        )
    if not declared:
        return actual, "extrusion in the file"
    if abs(declared - actual) / declared * 100.0 > cfg.header_mismatch_pct:
        return actual, "extrusion in the file, the header total being stale"
    return declared, "slicer header"


def rule_filament_remaining(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    if cfg.remaining_g is None:
        return []
    needed, source = estimated_grams(state, cfg)
    if needed <= 0:
        return []
    remaining = cfg.remaining_g
    material = material_of(state)
    density = material.density if material else DEFAULT_DENSITY

    if needed > remaining:
        used = 0.0
        elapsed = 0.0
        out_layer = None
        out_z = 0.0
        for layer in state.layers:
            grams = grams_from_mm(max(layer.extrusion_mm, 0.0), cfg.filament_diameter, density)
            if used + grams > remaining and out_layer is None:
                out_layer = layer.index
                out_z = layer.z
                break
            used += grams
            elapsed += layer.time_s
        where = ""
        if out_layer is not None:
            share = elapsed / state.totals.time_s if state.totals.time_s > 0 else 0.0
            where = (
                ", which is around layer %d of %d at z=%.2f mm"
                % (out_layer, state.layer_count, out_z)
            )
            if state.header.estimated_time_s:
                where += " (about %s in)" % format_duration(
                    share * state.header.estimated_time_s
                )
        return [
            Finding(
                rule="filament-remaining",
                number=11,
                severity="high",
                title="not enough filament on the spool",
                line=None,
                detail=(
                    "this file needs %s (%s) and you said %s is left, short by %s%s"
                    % (format_grams(needed), source, format_grams(remaining),
                       format_grams(needed - remaining), where)
                ),
                why=(
                    "a print that runs out stops where it stops. Printers that detect "
                    "the runout pause and hold position, and the resume seam shows; "
                    "printers that do not keep moving the head over the part for the "
                    "rest of the file."
                ),
                fix="load a fuller spool, or split the model and print it in parts.",
                data={"needed_g": round(needed, 1), "remaining_g": remaining,
                      "runs_out_layer": out_layer},
            )
        ]

    margin = remaining - needed
    if margin < max(needed * 0.1, 10.0):
        return [
            Finding(
                rule="filament-remaining",
                number=11,
                severity="low",
                title="finishes with very little filament to spare",
                line=None,
                detail=(
                    "this file needs %s (%s) and you said %s is left, a margin of %s"
                    % (format_grams(needed), source, format_grams(remaining),
                       format_grams(margin))
                ),
                why=(
                    "the estimate assumes the spool weight you gave is right and that "
                    "nothing goes wrong. A failed first layer that you restart, or a "
                    "spool that was weighed with its core, eats a margin this thin."
                ),
                fix="weigh the spool, subtract the core, or start with a fresh one.",
                data={"needed_g": round(needed, 1), "remaining_g": remaining,
                      "margin_g": round(margin, 1)},
            )
        ]
    return []


# -- 12 --------------------------------------------------------------------

def rule_header_consistency(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    header = state.header
    actual_mm = state.totals.extrusion_mm
    if header.estimated_filament_mm:
        declared, actual, unit = header.estimated_filament_mm, actual_mm, "mm"
    elif header.estimated_filament_g:
        material = material_of(state)
        density = material.density if material else DEFAULT_DENSITY
        declared = header.estimated_filament_g
        actual = grams_from_mm(actual_mm, cfg.filament_diameter, density)
        unit = "g"
    else:
        if actual_mm <= 0:
            return []
        return [
            Finding(
                rule="header-consistency",
                number=12,
                severity="low",
                title="no material estimate in the header to check against",
                line=None,
                detail=(
                    "the header carries no filament total, so the %.2f m this file "
                    "actually extrudes could not be cross-checked"
                    % (actual_mm / 1000.0)
                ),
                why=(
                    "the totals a slicer writes are the only record of what the file "
                    "was meant to do. Without them there is nothing to catch a file "
                    "that has been edited or concatenated since it was sliced."
                ),
                fix="re-export from the slicer rather than reusing a post-processed file.",
                data={"actual_mm": round(actual_mm, 1)},
            )
        ]

    if declared <= 0:
        return []
    diff = abs(actual - declared) / declared * 100.0
    if diff <= cfg.header_mismatch_pct:
        return []
    if unit == "mm":
        numbers = "the header says %.2f m of filament, the file extrudes %.2f m" % (
            declared / 1000.0, actual / 1000.0,
        )
    else:
        numbers = "the header says %.0f g of filament, the file extrudes %.0f g" % (
            declared, actual,
        )
    return [
        Finding(
            rule="header-consistency",
            number=12,
            severity="medium",
            title="header totals disagree with the file",
            line=None,
            detail="%s, a difference of %.0f percent" % (numbers, diff),
            why=(
                "the header block is written once at slicing time and nothing that "
                "edits the file afterwards updates it. A gap this wide means the "
                "numbers you are reading, print time included, describe a different "
                "version of this file: a post processing script, a hand edit, or two "
                "files concatenated."
            ),
            fix=(
                "re-slice rather than trusting the header, and treat the printed time "
                "estimate as unreliable until you do."
            ),
            data={"declared": round(declared, 2), "actual": round(actual, 2),
                  "unit": unit, "difference_pct": round(diff, 1)},
        )
    ]


RULES = (
    rule_first_layer_speed,
    rule_bed_adhesion,
    rule_temperature_range,
    rule_travel_retraction,
    rule_z_hop,
    rule_first_layer_fan,
    rule_cold_extrusion,
    rule_build_volume,
    rule_end_retraction,
    rule_chamber,
    rule_filament_remaining,
    rule_header_consistency,
)

RULE_INFO = (
    (1, "first-layer-speed", "First layer extrudes above the speed threshold."),
    (2, "bed-adhesion", "No skirt, brim, raft or purge line before the model."),
    (3, "temperature-range", "Nozzle or bed outside the window for the declared material."),
    (4, "travel-retraction", "Travel longer than the threshold with no retraction."),
    (5, "z-hop", "Travel crosses material already printed on the same layer, at layer height."),
    (6, "first-layer-fan", "Part cooling on the first layer for a material that needs it off."),
    (7, "cold-extrusion", "Extrusion before the hotend has reached temperature."),
    (8, "build-volume", "X, Y or Z leaves the printer's build volume."),
    (9, "end-retraction", "End gcode parks without retracting."),
    (10, "chamber", "No chamber temperature for a material that wants one."),
    (11, "filament-remaining", "Print needs more filament than --remaining says is left."),
    (12, "header-consistency", "Slicer header totals disagree with the file by over 10 percent."),
)


def run_rules(state: ParseResult, cfg: LintConfig) -> list[Finding]:
    """Run every rule and return findings worst first."""
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(state, cfg))
    findings.sort(key=lambda f: (severity_rank(f.severity), f.number, f.line or 0))
    return findings
