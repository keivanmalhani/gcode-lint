"""Streaming parser for sliced gcode.

The parser reads one line at a time and never holds the file in memory. It
produces a ParseResult made of two kinds of data:

* aggregates, which are always exact (totals, per layer stats, temperature
  extremes, coordinate extremes), and
* bounded evidence lists, which are capped so that a forty million line file
  costs the same memory as a small one. When a list is capped the result
  records how many entries were dropped.

The rule engine in rules.py reads a ParseResult and never touches the file,
so every rule is a pure function of this structure.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

MAX_EVENTS = 256
"""Default cap on each evidence list."""

CELL_MM = 4.0
"""Side of one occupancy grid cell. The grid records where material was laid
down on the current layer so that a travel move can be tested for passing
over it."""

_GRID_STRIDE = 8192
_CELL_INV = 1.0 / CELL_MM
_CELL_BIAS = 4096.0
"""Added to coordinates before the integer divide so that int() truncation
matches floor() without paying for math.floor on every sample."""

_SAMPLE_INV = 1.0 / (CELL_MM * 0.5)
_MAX_SAMPLES = 256
_EPS = 1e-4
_MIN_MOVE = 1e-3

_WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_ADHESION_WORDS = (
    "SKIRT",
    "BRIM",
    "RAFT",
    "PRIME TOWER",
    "PRIME-TOWER",
    "PRIMETOWER",
    "WIPE TOWER",
    "WIPE-TOWER",
    "WIPETOWER",
    "PURGE",
)

_MATERIAL_KEYS = (
    "PLA", "PETG", "PET", "PCTG", "ABS", "ASA", "TPU", "TPE", "PC", "PA",
    "PA6", "PA12", "PAHT", "NYLON", "PVA", "PVB", "HIPS", "PP", "PPS",
    "PPA", "PEEK", "PEI",
)

_MATERIAL_ALIASES = {
    "NYLON": "PA",
    "PA6": "PA",
    "PA12": "PA",
    "PAHT": "PA",
    "TPE": "TPU",
    "PET": "PETG",
}


class ParseError(Exception):
    """The file is not gcode this tool can read."""


@dataclass
class SlicerHeader:
    """Everything worth knowing that the slicer wrote in comments."""

    slicer: str | None = None
    slicer_version: str | None = None
    material: str | None = None
    material_raw: str | None = None
    printer_model: str | None = None
    estimated_time_s: float | None = None
    estimated_filament_mm: float | None = None
    estimated_filament_g: float | None = None
    nozzle_temp: float | None = None
    bed_temp: float | None = None
    chamber_temp: float | None = None
    layer_count: int | None = None

    def describe_slicer(self) -> str:
        if not self.slicer:
            return "unknown slicer"
        if self.slicer_version:
            return "%s %s" % (self.slicer, self.slicer_version)
        return self.slicer


@dataclass
class LayerStat:
    """One layer worth of motion. Index 0 is the start gcode."""

    index: int
    z: float = 0.0
    start_line: int = 0
    end_line: int = 0
    extrusion_mm: float = 0.0
    print_mm: float = 0.0
    travel_mm: float = 0.0
    time_s: float = 0.0
    max_speed: float = 0.0
    max_speed_line: int = 0
    moves: int = 0


@dataclass
class TravelEvent:
    """A travel move missing a retraction, a z hop, or both."""

    line: int
    length: float
    retracted: bool
    z_hop: bool
    crossed_print: bool
    layer: int


@dataclass
class TempEvent:
    """An M104/M109/M140/M190/M141/M191 command."""

    line: int
    code: str
    target: float
    wait: bool
    kind: str  # nozzle, bed or chamber


@dataclass
class FanEvent:
    """An M106/M107 command on the part cooling fan."""

    line: int
    percent: float
    layer: int


@dataclass
class ExtrudeEvent:
    """An extrusion made before the hotend was known to be at temperature."""

    line: int
    target: float | None
    waited: bool
    layer: int


@dataclass
class Extreme:
    """The largest or smallest value an axis or setting reached, and where."""

    value: float = 0.0
    line: int = 0
    seen: bool = False

    def offer_max(self, value: float, line: int) -> None:
        if not self.seen or value > self.value:
            self.value, self.line, self.seen = value, line, True

    def offer_min(self, value: float, line: int) -> None:
        if not self.seen or value < self.value:
            self.value, self.line, self.seen = value, line, True


@dataclass
class Totals:
    extrusion_mm: float = 0.0  # net filament consumed, retractions cancelled out
    retract_mm: float = 0.0
    print_mm: float = 0.0
    travel_mm: float = 0.0
    time_s: float = 0.0
    moves: int = 0
    commands: int = 0
    arcs: int = 0


@dataclass
class EndState:
    """What the file does after its last extrusion move."""

    last_extrusion_line: int = 0
    last_retract_line: int = 0
    first_move_after_print_line: int = 0
    retracted_at_end: bool = False
    retract_after_print_mm: float = 0.0


@dataclass
class ParseResult:
    """Everything the rules get to see."""

    source: str = "<stream>"
    header: SlicerHeader = field(default_factory=SlicerHeader)
    layers: list[LayerStat] = field(default_factory=list)
    start_gcode: LayerStat | None = None
    totals: Totals = field(default_factory=Totals)
    end: EndState = field(default_factory=EndState)
    line_count: int = 0

    travel_events: list[TravelEvent] = field(default_factory=list)
    travel_events_dropped: int = 0
    temp_events: list[TempEvent] = field(default_factory=list)
    temp_events_dropped: int = 0
    fan_events: list[FanEvent] = field(default_factory=list)
    fan_events_dropped: int = 0
    cold_extrude_events: list[ExtrudeEvent] = field(default_factory=list)
    cold_extrude_events_dropped: int = 0

    # Exact aggregates, unaffected by the evidence caps.
    nozzle_max: Extreme = field(default_factory=Extreme)
    nozzle_min: Extreme = field(default_factory=Extreme)
    bed_max: Extreme = field(default_factory=Extreme)
    bed_min: Extreme = field(default_factory=Extreme)
    chamber_max: Extreme = field(default_factory=Extreme)
    x_max: Extreme = field(default_factory=Extreme)
    x_min: Extreme = field(default_factory=Extreme)
    y_max: Extreme = field(default_factory=Extreme)
    y_min: Extreme = field(default_factory=Extreme)
    z_max: Extreme = field(default_factory=Extreme)
    first_layer_fan: Extreme = field(default_factory=Extreme)

    # Bed adhesion evidence.
    adhesion_feature: str | None = None
    adhesion_feature_line: int = 0
    first_object_feature: str | None = None
    first_object_feature_line: int = 0
    first_extrusion_line: int = 0
    pre_object_extrusion_mm: float = 0.0
    start_gcode_extrusion_mm: float = 0.0
    features_seen: list[tuple[int, str]] = field(default_factory=list)

    saw_layer_marker: bool = False
    relative_e: bool = False
    homed: bool = False
    inch_units: bool = False

    @property
    def first_layer(self) -> LayerStat | None:
        return self.layers[0] if self.layers else None

    @property
    def layer_count(self) -> int:
        return len(self.layers)


def parse_duration(text: str) -> float | None:
    """Read 1h 2m 3s, 45s, 2d 4h, or a bare number of seconds."""
    total = 0.0
    found = False
    for value, unit in _DURATION_RE.findall(text.lower()):
        scale = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        total += float(value) * scale
        found = True
    if found:
        return total
    match = _NUMBER_RE.search(text)
    return float(match.group(0)) if match else None


def format_duration(seconds: float | None) -> str:
    """Seconds as 1h 05m, 12m 30s or 45s."""
    if seconds is None:
        return "unknown"
    whole = int(round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def normalise_material(raw: str) -> str | None:
    """Reduce a slicer filament name to a bare material code.

    "Generic PLA", "Bambu PLA Basic @BBL X1C" and "PolyLite PLA+" all become
    PLA. Returns None when nothing recognisable is present.
    """
    if not raw:
        return None
    tokens = [t.rstrip("+") for t in re.split(r"[^A-Za-z0-9+]+", raw.upper()) if t]
    for token in tokens:
        if token in _MATERIAL_KEYS:
            return _MATERIAL_ALIASES.get(token, token)
    for token in tokens:
        for key in sorted(_MATERIAL_KEYS, key=len, reverse=True):
            if token.startswith(key) and len(token) <= len(key) + 3:
                return _MATERIAL_ALIASES.get(key, key)
    return None


def _cell_key(x: float, y: float) -> int:
    return (
        int((y + _CELL_BIAS) * _CELL_INV) * _GRID_STRIDE
        + int((x + _CELL_BIAS) * _CELL_INV)
    )


def _segment_cells(x0: float, y0: float, x1: float, y1: float):
    """Walk the occupancy cells a straight segment touches.

    Sampling twice per cell along the segment. Kept as a generator for the
    tests; the two hot callers inline the same arithmetic.
    """
    dx, dy = x1 - x0, y1 - y0
    steps = int(math.hypot(dx, dy) * _SAMPLE_INV) + 1
    if steps > _MAX_SAMPLES:
        steps = _MAX_SAMPLES
    for index in range(steps + 1):
        t = index / steps
        yield _cell_key(x0 + dx * t, y0 + dy * t)


class _Machine:
    """Modal gcode state plus the accumulators that become a ParseResult."""

    def __init__(self, source: str, max_events: int) -> None:
        self.max_events = max_events
        self.result = ParseResult(source=source)
        self.x = self.y = self.z = 0.0
        self.e = 0.0
        self.feed = 1200.0
        self.absolute = True
        self.absolute_e = True
        self.layer: LayerStat | None = None
        self.pending_layer_z: float | None = None
        self.layer_z_locked = False
        self.retracted = False
        self.z_hop_base: float | None = None
        self.hotend_target: float | None = None
        self.hotend_waited = False
        self.printed: set[int] = set()
        self.seen_command = False
        self.saw_object_feature = False
        self.post_print_move_seen = False
        self.m73_remaining_s: float | None = None

    def _append(self, items: list, event, counter: str) -> None:
        if len(items) < self.max_events:
            items.append(event)
        else:
            setattr(self.result, counter, getattr(self.result, counter) + 1)

    # -- layers -----------------------------------------------------------

    def start_layer(self, line: int, implicit: bool = False) -> None:
        self.close_layer(max(line - 1, 0))
        index = 0 if implicit else len(self.result.layers) + 1
        z = self.pending_layer_z if self.pending_layer_z is not None else self.z
        self.layer = LayerStat(index=index, z=z, start_line=line)
        self.layer_z_locked = self.pending_layer_z is not None
        self.pending_layer_z = None
        self.printed.clear()

    def close_layer(self, line: int) -> None:
        layer = self.layer
        if layer is None:
            return
        layer.end_line = line
        if layer.index == 0:
            if self.result.saw_layer_marker:
                # Extrusion before the first layer marker is start gcode: a
                # purge line or a prime blob, not a layer of the model.
                self.result.start_gcode = layer
                self.result.start_gcode_extrusion_mm += layer.extrusion_mm
            else:
                layer.index = 1
                self.result.layers.append(layer)
        else:
            self.result.layers.append(layer)
        self.layer = None

    @property
    def layer_index(self) -> int:
        return self.layer.index if self.layer is not None else 0

    # -- comments ---------------------------------------------------------

    def comment(self, text: str, line: int) -> None:
        header = self.result.header
        stripped = text.strip()
        if not stripped:
            return
        upper = stripped.upper()

        _detect_slicer(stripped, header)

        if (
            upper.startswith("LAYER:")
            or upper in ("LAYER_CHANGE", "CHANGE_LAYER")
            or upper.startswith("LAYER NUM/TOTAL_LAYER_COUNT:")
        ):
            self.result.saw_layer_marker = True
            self.start_layer(line)
            return
        if upper.startswith("Z:") or upper.startswith("Z_HEIGHT:"):
            value = _number(stripped)
            if value is not None:
                if self.layer is not None and self.layer.index > 0 and not self.layer_z_locked:
                    self.layer.z = value
                    self.layer_z_locked = True
                else:
                    self.pending_layer_z = value
            return
        if upper.startswith("TYPE:") or upper.startswith("FEATURE:"):
            self.feature(stripped.split(":", 1)[1].strip(), line)
            return

        for segment in stripped.split(";"):
            segment = segment.strip()
            if segment:
                _read_header_key(segment, header)

    def feature(self, name: str, line: int) -> None:
        result = self.result
        if len(result.features_seen) < self.max_events:
            result.features_seen.append((line, name))
        upper = name.upper()
        if any(word in upper for word in _ADHESION_WORDS):
            if result.adhesion_feature is None and not self.saw_object_feature:
                result.adhesion_feature = name
                result.adhesion_feature_line = line
            return
        if not self.saw_object_feature:
            self.saw_object_feature = True
            result.first_object_feature = name
            result.first_object_feature_line = line

    # -- motion -----------------------------------------------------------

    def move(self, words: dict[str, float], line: int, arc: bool = False) -> None:
        result = self.result
        start_x, start_y, start_z = self.x, self.y, self.z
        new_x, new_y, new_z = self.x, self.y, self.z
        if "X" in words:
            new_x = words["X"] if self.absolute else self.x + words["X"]
        if "Y" in words:
            new_y = words["Y"] if self.absolute else self.y + words["Y"]
        if "Z" in words:
            new_z = words["Z"] if self.absolute else self.z + words["Z"]
        if "F" in words and words["F"] > 0:
            self.feed = words["F"]

        de = 0.0
        if "E" in words:
            if self.absolute_e:
                de = words["E"] - self.e
                self.e = words["E"]
            else:
                de = words["E"]
                self.e += de

        dx, dy, dz = new_x - start_x, new_y - start_y, new_z - start_z
        planar = math.hypot(dx, dy)
        dist = math.sqrt(planar * planar + dz * dz)
        speed = self.feed / 60.0
        move_time = dist / speed if speed > _EPS and dist > 0 else 0.0

        self.x, self.y, self.z = new_x, new_y, new_z
        result.totals.moves += 1
        result.totals.time_s += move_time
        if arc:
            result.totals.arcs += 1  # chord length is used as the arc length

        if "X" in words:
            result.x_max.offer_max(new_x, line)
            result.x_min.offer_min(new_x, line)
        if "Y" in words:
            result.y_max.offer_max(new_y, line)
            result.y_min.offer_min(new_y, line)
        if "Z" in words:
            result.z_max.offer_max(new_z, line)

        if de > _EPS and dist > _MIN_MOVE:
            self._extrusion_move(de, dist, speed, line, start_x, start_y)
        elif de > _EPS:
            self.retracted = False
        elif de < -_EPS:
            self.retracted = True
            result.totals.retract_mm += -de
            result.end.last_retract_line = line
            if result.end.last_extrusion_line:
                result.end.retract_after_print_mm += -de
            if dist > _MIN_MOVE:
                self._travel_move(dist, planar, dz, line, start_x, start_y)
        elif dist > _MIN_MOVE:
            self._travel_move(dist, planar, dz, line, start_x, start_y)

        if abs(de) > _EPS:
            if self.layer is None:
                self.start_layer(line, implicit=True)
            result.totals.extrusion_mm += de
            self.layer.extrusion_mm += de
            if not self.saw_object_feature:
                result.pre_object_extrusion_mm += de
            if result.first_extrusion_line == 0 and de > 0:
                result.first_extrusion_line = line

        if self.layer is not None:
            self.layer.moves += 1
            self.layer.time_s += move_time

        self._track_hop(dz, planar, de)

    def _extrusion_move(
        self, de: float, dist: float, speed: float, line: int, x0: float, y0: float
    ) -> None:
        result = self.result
        self.retracted = False
        if self.layer is None:
            self.start_layer(line, implicit=True)
        elif (
            not result.saw_layer_marker
            and self.layer.extrusion_mm > 0
            and self.z > self.layer.z + 0.01
        ):
            # No slicer layer markers in this file, so a printing move at a
            # higher z is the only signal a new layer has started.
            self.start_layer(line)
        layer = self.layer
        assert layer is not None
        if not self.layer_z_locked:
            layer.z = self.z
            self.layer_z_locked = True

        result.totals.print_mm += dist
        layer.print_mm += dist
        if speed > layer.max_speed:
            layer.max_speed = speed
            layer.max_speed_line = line
        result.end.last_extrusion_line = line
        self.post_print_move_seen = False

        if self.hotend_target is None or self.hotend_target < 1.0 or not self.hotend_waited:
            self._append(
                result.cold_extrude_events,
                ExtrudeEvent(line, self.hotend_target, self.hotend_waited, layer.index),
                "cold_extrude_events_dropped",
            )

        # Inlined _segment_cells: this runs on every extrusion move in the
        # file, so it stays free of function calls and attribute lookups.
        add = self.printed.add
        dx, dy = self.x - x0, self.y - y0
        steps = int(dist * _SAMPLE_INV) + 1
        if steps > _MAX_SAMPLES:
            steps = _MAX_SAMPLES
        previous = -1
        for index in range(steps + 1):
            t = index / steps
            key = (
                int((y0 + dy * t + _CELL_BIAS) * _CELL_INV) * _GRID_STRIDE
                + int((x0 + dx * t + _CELL_BIAS) * _CELL_INV)
            )
            if key != previous:
                add(key)
                previous = key

    def _travel_move(
        self, dist: float, planar: float, dz: float, line: int, x0: float, y0: float
    ) -> None:
        result = self.result
        result.totals.travel_mm += dist
        if self.layer is not None:
            self.layer.travel_mm += dist
        if result.end.last_extrusion_line and not self.post_print_move_seen:
            result.end.first_move_after_print_line = line
            self.post_print_move_seen = True
        if planar <= _MIN_MOVE:
            return

        z_hop = self.z_hop_base is not None or dz > 0.01
        crossed = False
        printed = self.printed
        if printed:
            # The cells the travel starts and ends in are where the nozzle was
            # already sitting and is about to print, so only what it passes over
            # in between counts as dragging across the layer.
            first = _cell_key(x0, y0)
            last = _cell_key(self.x, self.y)
            dx, dy = self.x - x0, self.y - y0
            steps = int(planar * _SAMPLE_INV) + 1
            if steps > _MAX_SAMPLES:
                steps = _MAX_SAMPLES
            for index in range(steps + 1):
                t = index / steps
                key = (
                    int((y0 + dy * t + _CELL_BIAS) * _CELL_INV) * _GRID_STRIDE
                    + int((x0 + dx * t + _CELL_BIAS) * _CELL_INV)
                )
                if key != first and key != last and key in printed:
                    crossed = True
                    break
        if not self.retracted or (crossed and not z_hop):
            self._append(
                result.travel_events,
                TravelEvent(line, planar, self.retracted, z_hop, crossed, self.layer_index),
                "travel_events_dropped",
            )

    def _track_hop(self, dz: float, planar: float, de: float) -> None:
        if planar <= _MIN_MOVE and de <= _EPS and abs(dz) > 0.01:
            if dz > 0 and self.z_hop_base is None:
                self.z_hop_base = self.z - dz
            elif dz < 0 and self.z_hop_base is not None:
                if self.z <= self.z_hop_base + 0.01:
                    self.z_hop_base = None
        elif de > _EPS:
            self.z_hop_base = None

    # -- other commands ---------------------------------------------------

    def temperature(self, code: str, words: dict[str, float], line: int) -> None:
        result = self.result
        target = words.get("S", words.get("R"))
        if target is None:
            return
        wait = code in ("M109", "M190", "M191")
        kind = {
            "M104": "nozzle", "M109": "nozzle",
            "M140": "bed", "M190": "bed",
            "M141": "chamber", "M191": "chamber",
        }[code]
        self._append(
            result.temp_events, TempEvent(line, code, target, wait, kind), "temp_events_dropped"
        )
        if kind == "nozzle":
            if target > 0:
                result.nozzle_max.offer_max(target, line)
                result.nozzle_min.offer_min(target, line)
                self.hotend_target = target
                if wait:
                    self.hotend_waited = True
            else:
                self.hotend_target = 0.0
                self.hotend_waited = False
        elif kind == "bed":
            if target > 0:
                result.bed_max.offer_max(target, line)
                result.bed_min.offer_min(target, line)
        elif target > 0:
            result.chamber_max.offer_max(target, line)

    def fan(self, code: str, words: dict[str, float], line: int) -> None:
        if int(words.get("P", 1)) not in (0, 1):
            return  # auxiliary or chamber fan, not part cooling
        percent = 0.0 if code == "M107" else min(100.0, words.get("S", 255.0) / 2.55)
        self._append(
            self.result.fan_events, FanEvent(line, percent, self.layer_index),
            "fan_events_dropped",
        )
        if self.layer_index <= 1:
            self.result.first_layer_fan.offer_max(percent, line)

    def finish(self, line: int) -> ParseResult:
        self.close_layer(line)
        result = self.result
        result.line_count = line
        result.end.retracted_at_end = self.retracted
        result.relative_e = not self.absolute_e
        if result.header.estimated_time_s is None and self.m73_remaining_s is not None:
            result.header.estimated_time_s = self.m73_remaining_s
        return result


def _number(text: str) -> float | None:
    match = _NUMBER_RE.search(text)
    return float(match.group(0)) if match else None


def _detect_slicer(text: str, header: SlicerHeader) -> None:
    """Name the slicer, and fill in its version whenever a line carries one.

    Cura announces itself twice: ";FLAVOR:Marlin" on line one and
    ";Generated with Cura_SteamEngine 5.7.0" further down, so detection keeps
    looking until it has both a name and a version.
    """
    if header.slicer and header.slicer_version:
        return
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    name: str | None = None
    version: str | None = None
    if "orcaslicer" in compact:
        name, version = "OrcaSlicer", _version_after(lowered, "orcaslicer")
    elif "bambustudio" in compact:
        name, version = "Bambu Studio", _version_after(compact, "bambustudio")
    elif "prusaslicer" in compact:
        name, version = "PrusaSlicer", _version_after(lowered, "prusaslicer")
    elif "superslicer" in compact:
        name, version = "SuperSlicer", _version_after(lowered, "superslicer")
    elif "cura_steamengine" in lowered or "generated with cura" in lowered:
        name = "Cura"
        version = _version_after(lowered, "cura_steamengine") or _version_after(lowered, "cura")
    elif lowered.startswith("flavor:"):
        name = "Cura"
    if name is None:
        return
    if header.slicer is None:
        header.slicer, header.slicer_version = name, version
    elif header.slicer == name and header.slicer_version is None:
        header.slicer_version = version


def _version_after(text: str, marker: str) -> str | None:
    index = text.find(marker)
    if index < 0:
        return None
    tail = text[index + len(marker):].strip()
    match = re.match(r"[ v]*([0-9][0-9A-Za-z._+-]*)", tail)
    return match.group(1).rstrip(".,") if match else None


def _read_header_key(text: str, header: SlicerHeader) -> None:
    """Read the key/value comments the supported slicers write."""
    if "=" in text:
        key, _, value = text.partition("=")
        _apply_header(key.strip().lower(), value.strip(), header)
    if ":" in text:
        key, _, value = text.partition(":")
        _apply_header(key.strip().lower(), value.strip(), header)


def _apply_header(key: str, value: str, header: SlicerHeader) -> None:
    if not value:
        return
    if key in ("filament_type", "filament type", "material", "extruder_train.0.material.type"):
        if header.material is None:
            header.material_raw = value
            header.material = normalise_material(value)
    elif key in ("printer_model", "target_machine.name", "printer_settings_id", "machine_name"):
        if header.printer_model is None:
            header.printer_model = value
    elif key in (
        "estimated printing time (normal mode)",
        "estimated printing time",
        "total estimated time",
        "time",
    ):
        if header.estimated_time_s is None:
            header.estimated_time_s = parse_duration(value)
    elif key in ("filament used [mm]", "total filament length [mm]"):
        if header.estimated_filament_mm is None:
            header.estimated_filament_mm = _sum_numbers(value)
    elif key == "filament used":
        if header.estimated_filament_mm is None:
            total = _sum_numbers(value)
            if total is not None:
                header.estimated_filament_mm = total * (1000.0 if "m" in value else 1.0)
    elif key in ("filament used [g]", "total filament weight [g]", "filament weight"):
        if header.estimated_filament_g is None:
            header.estimated_filament_g = _sum_numbers(value)
    elif key in (
        "first_layer_temperature",
        "nozzle_temperature_initial_layer",
        "extruder_train.0.initial_temperature",
        "temperature",
        "nozzle_temperature",
    ):
        if header.nozzle_temp is None:
            header.nozzle_temp = _sum_numbers(value, first_only=True)
    elif key in (
        "first_layer_bed_temperature",
        "bed_temperature_initial_layer",
        "hot_plate_temp_initial_layer",
        "build_plate.initial_temperature",
        "bed_temperature",
        "hot_plate_temp",
    ):
        if header.bed_temp is None:
            header.bed_temp = _sum_numbers(value, first_only=True)
    elif key in ("chamber_temperature", "chamber_temperatures"):
        if header.chamber_temp is None:
            header.chamber_temp = _sum_numbers(value, first_only=True)
    elif key in ("layer_count", "total layer number", "total layers count"):
        if header.layer_count is None:
            number = _sum_numbers(value, first_only=True)
            header.layer_count = int(number) if number is not None else None


def _sum_numbers(value: str, first_only: bool = False) -> float | None:
    numbers = _NUMBER_RE.findall(value)
    if not numbers:
        return None
    if first_only:
        return float(numbers[0])
    return sum(float(n) for n in numbers)


def _words(code: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for letter, value in _WORD_RE.findall(code):
        letter = letter.upper()
        if letter not in out:
            out[letter] = float(value)
    return out


def parse_stream(
    lines: Iterable[str], source: str = "<stream>", max_events: int = MAX_EVENTS
) -> ParseResult:
    """Parse an iterable of gcode lines without materialising it."""
    machine = _Machine(source, max_events)
    result = machine.result
    lineno = 0
    for lineno, raw in enumerate(lines, start=1):
        if "\x00" in raw:
            raise ParseError(
                "%s looks like binary data, not text gcode" % source
            )
        semi = raw.find(";")
        if semi >= 0:
            code = raw[:semi]
            machine.comment(raw[semi + 1:].rstrip("\r\n"), lineno)
        else:
            code = raw
        code = code.strip()
        if not code:
            continue
        head = code.split(None, 1)[0].upper()
        if head[0] not in "GMT":
            continue
        machine.seen_command = True
        result.totals.commands += 1
        if head in ("G0", "G1"):
            machine.move(_words(code), lineno)
        elif head in ("G2", "G3"):
            machine.move(_words(code), lineno, arc=True)
        elif head == "G90":
            machine.absolute = True
        elif head == "G91":
            machine.absolute = False
        elif head == "M82":
            machine.absolute_e = True
        elif head == "M83":
            machine.absolute_e = False
        elif head == "G92":
            words = _words(code)
            if "E" in words:
                machine.e = words["E"]
            for axis in "XYZ":
                if axis in words:
                    setattr(machine, axis.lower(), words[axis])
        elif head == "G28":
            words = _words(code)
            for axis in [a for a in "XYZ" if a in words] or ["X", "Y", "Z"]:
                setattr(machine, axis.lower(), 0.0)
            result.homed = True
        elif head == "G10":
            machine.retracted = True
            result.end.last_retract_line = lineno
        elif head == "G11":
            machine.retracted = False
        elif head in ("M104", "M109", "M140", "M190", "M141", "M191"):
            machine.temperature(head, _words(code), lineno)
        elif head in ("M106", "M107"):
            machine.fan(head, _words(code), lineno)
        elif head == "G4":
            words = _words(code)
            result.totals.time_s += words.get("S", 0.0) + words.get("P", 0.0) / 1000.0
        elif head == "G20":
            result.inch_units = True
        elif head == "M73":
            # Prusa firmware progress. Only a fallback: the header comment is
            # more precise, and on PrusaSlicer output it comes later in the file.
            words = _words(code)
            if machine.m73_remaining_s is None and words.get("R"):
                machine.m73_remaining_s = words["R"] * 60.0
    if not machine.seen_command:
        raise ParseError("%s contains no gcode commands" % source)
    return machine.finish(lineno)


def parse_file(path: str, max_events: int = MAX_EVENTS) -> ParseResult:
    """Open a file and stream it through parse_stream."""
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace", newline="")
    except OSError as exc:
        raise ParseError("cannot read %s: %s" % (path, exc.strerror or exc)) from exc
    with handle:
        if handle.read(4) == "GCDE":
            raise ParseError(
                "%s is binary gcode (.bgcode); export plain text gcode to lint it" % path
            )
        handle.seek(0)
        return parse_stream(handle, source=path, max_events=max_events)
