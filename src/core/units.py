"""units — SI-canonical measurements plus user-defined local area-unit profiles.

Pure Python, no UI imports. Canonical storage is always SI (metres, square
metres); everything here is a **display/export-time conversion only** — it never
changes how geometry is measured or stored (PROJECT_BRIEF "Measurement & unit
handling").

Two fixed, universally-correct built-in tables (exact factors, hardcoded once):

  * **area** — square metre, square foot, acre, hectare;
  * **length** — metre, foot (the linear counterparts for perimeter / segment
    lengths).

Local measures (Bigha, Biswa, ...) are NOT hardcoded — they vary by
state/district, so there is no safe universal factor. They are user-defined
**area** profiles (``{name, sq_m_per_unit}``) stored in the project DB and
selected per source; a local profile converts *area* only (khasra areas are
areal), so perimeter and segment lengths always report in SI plus the built-in
linear units.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Built-in AREA units: display name -> exact square metres per 1 unit.
#: 1 ft = 0.3048 m exactly, so 1 sq ft = 0.3048**2; 1 acre = 43560 sq ft.
BUILTIN_AREA_UNITS: dict[str, float] = {
    "square metre": 1.0,
    "square foot": 0.09290304,
    "acre": 4046.8564224,
    "hectare": 10000.0,
}

#: Built-in LENGTH units: display name -> exact metres per 1 unit.
BUILTIN_LENGTH_UNITS: dict[str, float] = {
    "metre": 1.0,
    "foot": 0.3048,
}

#: The name seeded as the SI area unit — used to recognise the trivial profile.
SI_AREA_UNIT = "square metre"


@dataclass(frozen=True)
class UnitProfile:
    """A named area unit as a factor to square metres. Built-ins and user-defined
    local profiles share this shape; ``is_builtin`` marks the fixed ones that
    cannot be edited or deleted."""

    name: str
    sq_m_per_unit: float
    is_builtin: bool = False
    id: int | None = None

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("unit profile name must be non-empty")
        if not (self.sq_m_per_unit > 0):
            raise ValueError(
                f"sq_m_per_unit must be positive, got {self.sq_m_per_unit!r}")


def area_in_unit(area_sq_m: float, sq_m_per_unit: float) -> float:
    """Convert an SI area (square metres) into a unit whose size is
    *sq_m_per_unit* square metres. Raises for a non-positive factor."""
    if not (sq_m_per_unit > 0):
        raise ValueError(f"sq_m_per_unit must be positive, got {sq_m_per_unit!r}")
    return area_sq_m / sq_m_per_unit


def length_in_unit(length_m: float, m_per_unit: float) -> float:
    """Convert an SI length (metres) into a unit whose size is *m_per_unit*
    metres. Raises for a non-positive factor."""
    if not (m_per_unit > 0):
        raise ValueError(f"m_per_unit must be positive, got {m_per_unit!r}")
    return length_m / m_per_unit


def area_in_profile(area_sq_m: float, profile: UnitProfile) -> float:
    """Convenience: convert an SI area into *profile*'s unit."""
    return area_in_unit(area_sq_m, profile.sq_m_per_unit)
