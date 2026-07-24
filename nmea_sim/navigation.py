"""Geodesy and coordinate formatting for NMEA output.

Two jobs:

* **Dead reckoning** — advance a position along a course at a speed for a time step,
  using ``geographiclib`` (WGS-84, pure-python, zero-dep) so the motion is
  geodetically correct rather than a flat-earth approximation.
* **Coordinate formatting** — convert signed decimal degrees to the NMEA
  ``ddmm.mmmm`` / ``dddmm.mmmm`` + hemisphere representation.
"""

from __future__ import annotations

from geographiclib.geodesic import Geodesic

_GEOD = Geodesic.WGS84

# 1 knot = 1 nautical mile (1852 m) per hour (3600 s).
KNOTS_TO_MPS = 1852.0 / 3600.0


def knots_to_mps(sog_kn: float) -> float:
    """Convert speed in knots to metres per second."""
    return sog_kn * KNOTS_TO_MPS


def dead_reckon(
    lat: float, lon: float, sog_kn: float, cog_deg: float, dt_s: float
) -> tuple[float, float]:
    """Advance ``(lat, lon)`` along course ``cog_deg`` at ``sog_kn`` for ``dt_s`` seconds.

    Returns the new ``(lat, lon)``. A zero distance returns the input point unchanged.
    """
    distance_m = knots_to_mps(sog_kn) * dt_s
    if distance_m == 0.0:
        return lat, lon
    result = _GEOD.Direct(lat, lon, cog_deg, distance_m)
    return result["lat2"], result["lon2"]


def to_ddmm(value: float, is_lat: bool) -> tuple[str, str]:
    """Format signed decimal degrees as ``(ddmm.mmmm, hemisphere)``.

    Latitude uses 2 degree digits and N/S; longitude uses 3 and E/W. A float-rounding
    edge that would render minutes as ``60.0000`` carries into the degrees instead.
    """
    if is_lat:
        hemisphere = "N" if value >= 0 else "S"
        degree_width = 2
    else:
        hemisphere = "E" if value >= 0 else "W"
        degree_width = 3

    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60.0

    # Guard: %.4f rounding of e.g. 59.999997 must not emit "60.0000". Minutes are
    # always < 60 mathematically, so the only trigger is rounding — carry to a whole
    # degree and reset minutes to exactly zero.
    if round(minutes, 4) >= 60.0:
        degrees += 1
        minutes = 0.0

    return f"{degrees:0{degree_width}d}{minutes:07.4f}", hemisphere
