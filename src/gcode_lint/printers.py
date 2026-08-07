"""Printer presets and build volume handling.

A build volume is three numbers in millimetres: X travel, Y travel and Z
travel. The presets below are the usable print area published by each
vendor, not the physical bed dimension, so a model that fits the preset
fits the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Enclosure kinds. "open" has no walls, "passive" is enclosed but has no
#: chamber heater, "active" has a heater that gcode can command.
ENCLOSURE_KINDS = ("open", "passive", "active")


@dataclass(frozen=True)
class Printer:
    """A build volume plus the few traits the rules care about."""

    key: str
    name: str
    x: float
    y: float
    z: float
    enclosure: str = "open"

    @property
    def volume(self) -> str:
        return "%gx%gx%g" % (self.x, self.y, self.z)

    def describe(self) -> str:
        return "%s (%s mm, %s enclosure)" % (self.name, self.volume, self.enclosure)

    def over(self, axis: str, value: float) -> float:
        """Return how far past the limit a coordinate sits, or 0.0."""
        limit = {"x": self.x, "y": self.y, "z": self.z}[axis]
        return max(0.0, value - limit)


PRINTERS: dict[str, Printer] = {
    "bambu-a1": Printer("bambu-a1", "Bambu Lab A1", 256, 256, 256, "open"),
    "bambu-p1s": Printer("bambu-p1s", "Bambu Lab P1S", 256, 256, 256, "passive"),
    "bambu-x1c": Printer("bambu-x1c", "Bambu Lab X1 Carbon", 256, 256, 256, "passive"),
    "prusa-mk4": Printer("prusa-mk4", "Prusa MK4", 250, 210, 220, "open"),
    "ender3": Printer("ender3", "Creality Ender 3", 220, 220, 250, "open"),
}

#: Spellings people actually type, and the model names slicers write into
#: the header, mapped onto preset keys.
ALIASES: dict[str, str] = {
    "a1": "bambu-a1",
    "bambu a1": "bambu-a1",
    "bambu lab a1": "bambu-a1",
    "p1s": "bambu-p1s",
    "bambu p1s": "bambu-p1s",
    "bambu lab p1s": "bambu-p1s",
    "x1c": "bambu-x1c",
    "x1 carbon": "bambu-x1c",
    "bambu x1c": "bambu-x1c",
    "bambu lab x1 carbon": "bambu-x1c",
    "bambu lab x1-carbon": "bambu-x1c",
    "mk4": "prusa-mk4",
    "mk4s": "prusa-mk4",
    "mk4is": "prusa-mk4",
    "prusa mk4": "prusa-mk4",
    "original prusa mk4": "prusa-mk4",
    "original prusa mk4 input shaper": "prusa-mk4",
    "ender-3": "ender3",
    "ender 3": "ender3",
    "creality ender-3": "ender3",
    "creality ender 3": "ender3",
}

_BED_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*$")


class PrinterError(ValueError):
    """Raised when a printer name or bed specification cannot be used."""


def _normalise(name: str) -> str:
    return re.sub(r"[_\s]+", " ", name.strip().lower())


def resolve_printer(name: str) -> Printer:
    """Look up a preset by key or alias."""
    key = _normalise(name)
    if key in PRINTERS:
        return PRINTERS[key]
    if key in ALIASES:
        return PRINTERS[ALIASES[key]]
    hyphenated = key.replace(" ", "-")
    if hyphenated in PRINTERS:
        return PRINTERS[hyphenated]
    if hyphenated in ALIASES:
        return PRINTERS[ALIASES[hyphenated]]
    raise PrinterError(
        "unknown printer %r; known presets: %s" % (name, ", ".join(sorted(PRINTERS)))
    )


def parse_bed(spec: str) -> Printer:
    """Turn a WxHxD string such as 256x256x256 into a Printer."""
    match = _BED_RE.match(spec)
    if not match:
        raise PrinterError("bad --bed value %r; expected WxHxD, for example 250x210x220" % spec)
    x, y, z = (float(v) for v in match.groups())
    if min(x, y, z) <= 0:
        raise PrinterError("bad --bed value %r; all three numbers must be positive" % spec)
    return Printer("custom", "custom bed", x, y, z, "open")


def match_model(model: str | None) -> Printer | None:
    """Best effort match of a slicer header printer_model onto a preset."""
    if not model:
        return None
    key = _normalise(model)
    for candidate in (key, key.replace(" ", "-")):
        if candidate in PRINTERS:
            return PRINTERS[candidate]
        if candidate in ALIASES:
            return PRINTERS[ALIASES[candidate]]
    for alias, target in ALIASES.items():
        if alias in key and len(alias) > 3:
            return PRINTERS[target]
    return None


def preset_table() -> list[tuple[str, str, str, str]]:
    """Rows of (key, name, volume, enclosure) for help text and the README."""
    return [
        (p.key, p.name, p.volume + " mm", p.enclosure)
        for p in sorted(PRINTERS.values(), key=lambda p: p.key)
    ]
