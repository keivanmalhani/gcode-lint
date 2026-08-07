"""Gcode fixtures, generated rather than committed.

build() writes a small multi layer cube in the header dialect of one of the
four supported slicers. Every defect the linter looks for is a keyword
argument, so a test can turn on exactly one of them and assert that exactly
one rule fires. The defaults produce a file that no rule complains about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DIALECTS = ("prusaslicer", "cura", "orcaslicer", "bambustudio")

PRINTER_MODEL = {
    "prusaslicer": "MK4",
    "cura": "Creality Ender-3",
    "orcaslicer": "Bambu Lab X1 Carbon",
    "bambustudio": "Bambu Lab P1S",
}

FEATURE_NAMES = {
    "prusaslicer": {"skirt": "Skirt", "wall": "External perimeter", "fill": "Solid infill"},
    "cura": {"skirt": "SKIRT", "wall": "WALL-OUTER", "fill": "FILL"},
    "orcaslicer": {"skirt": "Skirt", "wall": "Outer wall", "fill": "Sparse infill"},
    "bambustudio": {"skirt": "Skirt", "wall": "Outer wall", "fill": "Sparse infill"},
}


@dataclass
class Options:
    dialect: str = "prusaslicer"
    material: str = "PLA"
    layers: int = 8
    layer_height: float = 0.2
    size: float = 40.0
    gap: float = 20.0
    origin_x: float = 60.0
    origin_y: float = 60.0
    first_layer_speed: float = 25.0
    speed: float = 60.0
    travel_speed: float = 150.0
    nozzle: float = 215.0
    bed: float = 60.0
    chamber: float | None = None
    line_width: float = 0.42
    filament_diameter: float = 1.75

    # defects, all off by default
    skirt: bool = True
    purge_line: bool = True
    retract: bool = True
    z_hop: bool = True
    end_retract: bool = True
    wait_for_temp: bool = True
    set_temp: bool = True
    fan_first_layer: float = 0.0
    fan_later: float = 100.0
    header_filament_mm: float | None = None
    header_time_s: float | None = None
    declare_material: bool = True
    x_offset: float = 0.0

    @property
    def e_per_mm(self) -> float:
        area = math.pi * (self.filament_diameter / 2.0) ** 2
        return self.layer_height * self.line_width / area


class _Writer:
    """Emits gcode while tracking the state a slicer would track."""

    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self.lines: list[str] = []
        self.x = self.y = self.z = 0.0
        self.e = 0.0
        self.total_e = 0.0
        self.time_s = 0.0
        self.relative_e = opts.dialect != "cura"
        self.retracted = False

    def emit(self, line: str) -> None:
        self.lines.append(line)

    def _advance(self, dist: float, speed: float) -> None:
        if speed > 0:
            self.time_s += dist / speed

    def extrude(self, x: float, y: float, speed: float) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        de = dist * self.opts.e_per_mm
        self.total_e += de
        if self.relative_e:
            self.emit("G1 X%.3f Y%.3f E%.5f F%.0f" % (x, y, de, speed * 60))
        else:
            self.e += de
            self.emit("G1 X%.3f Y%.3f E%.5f F%.0f" % (x, y, self.e, speed * 60))
        self.x, self.y = x, y
        self._advance(dist, speed)

    def retract(self, amount: float = 0.8) -> None:
        if self.relative_e:
            self.emit("G1 E-%.5f F2400" % amount)
        else:
            self.e -= amount
            self.emit("G1 F2400 E%.5f" % self.e)
        self.retracted = True

    def unretract(self, amount: float = 0.8) -> None:
        if self.relative_e:
            self.emit("G1 E%.5f F2400" % amount)
        else:
            self.e += amount
            self.emit("G1 F2400 E%.5f" % self.e)
        self.retracted = False

    def lift(self, dz: float) -> None:
        self.z += dz
        self.emit("G1 Z%.3f F9000" % self.z)

    def move_z(self, z: float) -> None:
        self.z = z
        self.emit("G1 Z%.3f F720" % z)

    def travel(self, x: float, y: float, retract: bool | None = None,
               hop: bool | None = None) -> None:
        opts = self.opts
        do_retract = opts.retract if retract is None else retract
        do_hop = opts.z_hop if hop is None else hop
        dist = math.hypot(x - self.x, y - self.y)
        if do_retract:
            self.retract()
        if do_hop:
            self.lift(0.4)
        self.emit("G1 X%.3f Y%.3f F%.0f" % (x, y, opts.travel_speed * 60))
        self.x, self.y = x, y
        self._advance(dist, opts.travel_speed)
        if do_hop:
            self.lift(-0.4)
        if do_retract:
            self.unretract()


def _feature(writer: _Writer, kind: str) -> None:
    name = FEATURE_NAMES[writer.opts.dialect][kind]
    if writer.opts.dialect in ("orcaslicer", "bambustudio"):
        writer.emit("; FEATURE: %s" % name)
    else:
        writer.emit(";TYPE:%s" % name)


def _layer_marker(writer: _Writer, index: int, z: float, total: int) -> None:
    dialect = writer.opts.dialect
    if dialect == "prusaslicer":
        writer.emit(";LAYER_CHANGE")
        writer.emit(";Z:%.2f" % z)
        writer.emit(";HEIGHT:%.2f" % writer.opts.layer_height)
    elif dialect == "cura":
        writer.emit(";LAYER:%d" % (index - 1))
        writer.emit("G92 E0")
        writer.e = 0.0
    elif dialect == "orcaslicer":
        writer.emit("; CHANGE_LAYER")
        writer.emit("; Z_HEIGHT: %.2f" % z)
        writer.emit("; LAYER_HEIGHT: %.2f" % writer.opts.layer_height)
    else:
        writer.emit("; layer num/total_layer_count: %d/%d" % (index, total))
        writer.emit("; Z_HEIGHT: %.2f" % z)


def _island(writer: _Writer, x0: float, y0: float, speed: float) -> None:
    """A square perimeter with zigzag infill that ends on the left edge."""
    size = writer.opts.size
    writer.travel(x0, y0)
    _feature(writer, "wall")
    writer.extrude(x0 + size, y0, speed)
    writer.extrude(x0 + size, y0 + size, speed)
    writer.extrude(x0, y0 + size, speed)
    writer.extrude(x0, y0, speed)
    _feature(writer, "fill")
    writer.travel(x0 + 1.0, y0 + 1.0, retract=False, hop=False)
    passes = 4
    step = (size - 2.0) / (2.0 * passes)
    y = y0 + 1.0
    for _ in range(passes):
        writer.extrude(x0 + size - 1.0, y, speed)
        y += step
        writer.extrude(x0 + size - 1.0, y, speed)
        writer.extrude(x0 + 1.0, y, speed)
        y += step
        writer.extrude(x0 + 1.0, y, speed)


def _start_gcode(writer: _Writer) -> None:
    opts = writer.opts
    writer.emit("G90")
    writer.emit("M82 ; absolute extrusion" if opts.dialect == "cura" else "M83")
    writer.emit("M140 S%.0f" % opts.bed)
    writer.emit("M190 S%.0f" % opts.bed)
    if opts.chamber is not None:
        writer.emit("M141 S%.0f" % opts.chamber)
    if opts.set_temp:
        writer.emit("M104 S%.0f" % opts.nozzle)
        if opts.wait_for_temp:
            writer.emit("M109 S%.0f" % opts.nozzle)
    writer.emit("G28 ; home")
    writer.emit("G92 E0")
    writer.e = 0.0
    writer.emit("M107")
    writer.move_z(opts.layer_height)
    if opts.purge_line:
        writer.emit("G1 X5.000 Y20.000 F3000")
        writer.x, writer.y = 5.0, 20.0
        writer.extrude(5.0, 180.0, 20.0)
        writer.extrude(5.4, 180.0, 20.0)
        writer.extrude(5.4, 20.0, 20.0)


def _end_gcode(writer: _Writer) -> None:
    if writer.opts.end_retract:
        writer.retract(2.0)
    writer.emit("M104 S0")
    writer.emit("M140 S0")
    writer.emit("M107")
    writer.lift(10.0)
    writer.emit("G1 X0.000 Y200.000 F3000")
    writer.x, writer.y = 0.0, 200.0
    writer.emit("M84 ; steppers off")


def _header(opts: Options, total_e: float, time_s: float, layers: int) -> list[str]:
    filament_mm = opts.header_filament_mm if opts.header_filament_mm is not None else total_e
    seconds = opts.header_time_s if opts.header_time_s is not None else time_s
    grams = filament_mm * math.pi * (opts.filament_diameter / 2.0) ** 2 / 1000.0 * 1.24
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    duration = ("%dh %dm %ds" % (hours, minutes, secs)) if hours else ("%dm %ds" % (minutes, secs))
    model = PRINTER_MODEL[opts.dialect]
    material = opts.material

    if opts.dialect == "prusaslicer":
        head = [
            "; generated by PrusaSlicer 2.7.4+linux on 2026-01-14 at 09:12:33 UTC",
            ";",
            "; total layers count = %d" % layers,
            "M73 P0 R%d" % max(1, int(seconds // 60)),
            "M201 X4000 Y4000 Z200 E2500",
            "M204 P4000 R1200 T4000",
            'M862.3 P "%s" ; printer model check' % model,
            "M115 U2.0.0",
        ]
        tail = [
            "",
            "; filament used [mm] = %.2f" % filament_mm,
            "; filament used [cm3] = %.2f" % (filament_mm * 0.0024053),
            "; filament used [g] = %.2f" % grams,
            "; estimated printing time (normal mode) = %s" % duration,
            "; prusaslicer_config = begin",
        ]
        if opts.declare_material:
            tail.append("; filament_type = %s" % material)
        tail += [
            "; first_layer_temperature = %.0f" % opts.nozzle,
            "; first_layer_bed_temperature = %.0f" % opts.bed,
            "; chamber_temperature = %.0f" % (opts.chamber or 0),
            "; printer_model = %s" % model,
            "; prusaslicer_config = end",
        ]
        return head, tail

    if opts.dialect == "cura":
        head = [
            ";FLAVOR:Marlin",
            ";TIME:%d" % int(seconds),
            ";Filament used: %.5fm" % (filament_mm / 1000.0),
            ";Layer height: %.2f" % opts.layer_height,
            ";MINX:%.3f" % (opts.origin_x + opts.x_offset),
            ";MINY:%.3f" % opts.origin_y,
            ";MINZ:%.2f" % opts.layer_height,
            ";MAXX:%.3f" % (opts.origin_x + opts.x_offset + 2 * opts.size + opts.gap),
            ";MAXY:%.3f" % (opts.origin_y + opts.size),
            ";MAXZ:%.2f" % (opts.layers * opts.layer_height),
            ";TARGET_MACHINE.NAME:%s" % model,
            ";Generated with Cura_SteamEngine 5.7.0",
            ";EXTRUDER_TRAIN.0.INITIAL_TEMPERATURE:%.0f" % opts.nozzle,
            ";BUILD_PLATE.INITIAL_TEMPERATURE:%.0f" % opts.bed,
        ]
        if opts.declare_material:
            head.append(";EXTRUDER_TRAIN.0.MATERIAL.TYPE:%s" % material)
        head.append(";LAYER_COUNT:%d" % layers)
        return head, [";End of Gcode"]

    if opts.dialect == "orcaslicer":
        head = [
            "; generated by OrcaSlicer 2.0.0 on 2026-01-14 at 09:12:33",
            "; total layer number: %d" % layers,
            "; total filament length [mm] : %.2f" % filament_mm,
            "; total filament volume [cm^3] : %.2f" % (filament_mm * 0.0024053),
            "; total filament weight [g] : %.2f" % grams,
            "; model printing time: %s; total estimated time: %s" % (duration, duration),
        ]
        if opts.declare_material:
            head.append("; filament_type = %s" % material)
        head += [
            "; nozzle_temperature_initial_layer = %.0f" % opts.nozzle,
            "; hot_plate_temp_initial_layer = %.0f" % opts.bed,
            "; chamber_temperature = %.0f" % (opts.chamber or 0),
            "; printer_model = %s" % model,
        ]
        return head, ["; EXECUTABLE_BLOCK_END"]

    head = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 01.09.00.65",
        "; model printing time: %s; total estimated time: %s" % (duration, duration),
        "; total layer number: %d" % layers,
        "; total filament length [mm] : %.2f" % filament_mm,
        "; total filament weight [g] : %.2f" % grams,
        "; filament_diameter: %.2f" % opts.filament_diameter,
        "; HEADER_BLOCK_END",
        "",
        "; CONFIG_BLOCK_START",
    ]
    if opts.declare_material:
        head.append("; filament_type = %s" % material)
    head += [
        "; nozzle_temperature_initial_layer = %.0f" % opts.nozzle,
        "; hot_plate_temp_initial_layer = %.0f" % opts.bed,
        "; chamber_temperature = %.0f" % (opts.chamber or 0),
        "; printer_model = %s" % PRINTER_MODEL["bambustudio"],
        "; CONFIG_BLOCK_END",
        "",
        "; EXECUTABLE_BLOCK_START",
    ]
    return head, ["; EXECUTABLE_BLOCK_END"]


def build(**kwargs) -> str:
    """Generate one gcode file as text."""
    opts = Options(**kwargs)
    if opts.dialect not in DIALECTS:
        raise ValueError("unknown dialect %r" % opts.dialect)
    writer = _Writer(opts)
    _start_gcode(writer)

    x0 = opts.origin_x + opts.x_offset
    xb = x0 + opts.size + opts.gap
    for index in range(1, opts.layers + 1):
        z = round(index * opts.layer_height, 3)
        _layer_marker(writer, index, z, opts.layers)
        writer.move_z(z)
        speed = opts.first_layer_speed if index == 1 else opts.speed
        if index == 1:
            if opts.fan_first_layer > 0:
                writer.emit("M106 S%.0f" % (opts.fan_first_layer * 2.55))
            else:
                writer.emit("M107")
            if opts.skirt:
                _feature(writer, "skirt")
                writer.travel(x0 - 4.0, opts.origin_y - 4.0)
                span = 2 * opts.size + opts.gap + 8.0
                writer.extrude(x0 - 4.0 + span, opts.origin_y - 4.0, speed)
                writer.extrude(x0 - 4.0 + span, opts.origin_y + opts.size + 4.0, speed)
                writer.extrude(x0 - 4.0, opts.origin_y + opts.size + 4.0, speed)
                writer.extrude(x0 - 4.0, opts.origin_y - 4.0, speed)
        elif index == 2:
            writer.emit("M106 S%.0f" % (opts.fan_later * 2.55))
        _island(writer, x0, opts.origin_y, speed)
        _island(writer, xb, opts.origin_y, speed)

    _end_gcode(writer)
    head, tail = _header(opts, writer.total_e, writer.time_s, opts.layers)
    return "\n".join(head + writer.lines + tail) + "\n"


def write(path, **kwargs) -> str:
    """Generate a file on disk and return its path."""
    text = build(**kwargs)
    with open(path, "w", encoding="ascii") as handle:
        handle.write(text)
    return str(path)


def large_file(path, target_lines: int = 500_000) -> int:
    """Write a valid gcode file of at least target_lines lines.

    The shape matches real slicer output: a fine perimeter of short segments,
    then solid infill rows separated by retract, lift, travel. Every fifth row
    travels without retracting so the parser's evidence caps get exercised.
    """
    cx, cy, radius = 110.0, 105.0, 40.0
    perimeter_points = 700
    infill_rows = 50
    written = 0
    with open(path, "w", encoding="ascii") as handle:
        head = [
            "; generated by PrusaSlicer 2.7.4+linux on 2026-01-14 at 09:12:33 UTC",
            "; filament_type = PLA",
            "; printer_model = MK4",
            "G90",
            "M83",
            "M140 S60",
            "M190 S60",
            "M104 S215",
            "M109 S215",
            "G28",
            "G92 E0",
            "G1 Z0.200 F720",
            "G1 X5.000 Y20.000 F3000",
            "G1 X5.000 Y180.000 E5.60000 F1200",
        ]
        handle.write("\n".join(head) + "\n")
        written += len(head)
        ring = [
            (cx + radius * math.cos(2 * math.pi * i / perimeter_points),
             cy + radius * math.sin(2 * math.pi * i / perimeter_points))
            for i in range(perimeter_points)
        ]
        layer = 0
        z = 0.2
        while written < target_lines:
            layer += 1
            z = round(0.2 * layer, 3)
            block = [";LAYER_CHANGE", ";Z:%.2f" % z, "G1 Z%.3f F720" % z,
                     ";TYPE:External perimeter"]
            for x, y in ring:
                block.append("G1 X%.3f Y%.3f E%.5f F1800" % (x, y, 0.01252))
            block.append(";TYPE:Solid infill")
            y = cy - 30.0
            for row in range(infill_rows):
                y += 1.2
                retracting = row % 5 != 0
                if retracting:
                    block.append("G1 E-0.80000 F2400")
                block.append("G1 Z%.3f F9000" % (z + 0.4))
                block.append("G1 X%.3f Y%.3f F9000" % (cx - 30.0, y))
                block.append("G1 Z%.3f F9000" % z)
                if retracting:
                    block.append("G1 E0.80000 F2400")
                block.append("G1 X%.3f Y%.3f E2.10000 F1800" % (cx + 30.0, y))
            handle.write("\n".join(block) + "\n")
            written += len(block)
        tail = ["G1 E-2.00000 F2400", "M104 S0", "M140 S0",
                "G1 Z%.3f F600" % (z + 10), "M84"]
        handle.write("\n".join(tail) + "\n")
        written += len(tail)
    return written
