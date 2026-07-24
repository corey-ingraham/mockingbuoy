"""GPS sentence generation: GGA, RMC, VTG, ZDA, GLL.

Sentences are built with ``pynmea2`` (which appends the checksum on ``str()``), so the
talker ID is the first constructor argument. Every sentence is returned **without** a
line ending — the serial layer appends ``\\r\\n``.

CORRECTNESS: RMC's true course and VTG's true track are **course-over-ground**
(``cog_deg``), never heading. Heading lives in the HDT/HDG/HDM sentences (P2). Magnetic
values derive from one signed variation (East-positive): ``magnetic = true - variation``.
"""

from __future__ import annotations

from datetime import datetime

import pynmea2

from .navigation import to_ddmm
from .state import VesselState

# Sentences this generator knows how to build.
SUPPORTED = ("GGA", "RMC", "VTG", "ZDA", "GLL")


def _timestamp(utc: datetime) -> str:
    """UTC time as ``hhmmss.ss`` (centiseconds)."""
    return f"{utc:%H%M%S}.{utc.microsecond // 10000:02d}"


def _datestamp(utc: datetime) -> str:
    """UTC date as ``ddmmyy``."""
    return f"{utc:%d%m%y}"


def _magnetic(true_deg: float, variation_deg: float) -> float:
    """Magnetic bearing from a true bearing and East-positive variation."""
    return (true_deg - variation_deg) % 360.0


class GpsGenerator:
    """Builds GPS sentences for one talker (default ``GP``) from a ``VesselState``."""

    def __init__(self, talker: str = "GP") -> None:
        self.talker = talker

    def build(self, state: VesselState, sentences: tuple[str, ...] = SUPPORTED) -> list[str]:
        """Return the requested sentences (in order) as strings without CRLF."""
        builders = {
            "GGA": self.gga,
            "RMC": self.rmc,
            "VTG": self.vtg,
            "ZDA": self.zda,
            "GLL": self.gll,
        }
        out: list[str] = []
        for name in sentences:
            try:
                out.append(builders[name](state))
            except KeyError:
                raise ValueError(f"unsupported GPS sentence {name!r}") from None
        return out

    def gga(self, s: VesselState) -> str:
        lat, lat_dir = to_ddmm(s.lat, is_lat=True)
        lon, lon_dir = to_ddmm(s.lon, is_lat=False)
        msg = pynmea2.GGA(
            self.talker,
            "GGA",
            (
                _timestamp(s.utc),
                lat,
                lat_dir,
                lon,
                lon_dir,
                str(s.fix_quality),
                f"{s.satellites:02d}",
                f"{s.hdop:.1f}",
                f"{s.altitude_m:.1f}",
                "M",
                "0.0",
                "M",
                "",
                "",
            ),
        )
        return str(msg)

    def rmc(self, s: VesselState) -> str:
        lat, lat_dir = to_ddmm(s.lat, is_lat=True)
        lon, lon_dir = to_ddmm(s.lon, is_lat=False)
        status = "A" if s.fix_quality > 0 else "V"  # A=valid, V=warning (no fix)
        var_dir = "E" if s.mag_variation_deg >= 0 else "W"
        msg = pynmea2.RMC(
            self.talker,
            "RMC",
            (
                _timestamp(s.utc),
                status,
                lat,
                lat_dir,
                lon,
                lon_dir,
                f"{s.sog_kn:.1f}",
                f"{s.cog_deg:.1f}",  # true course = COG, NOT heading
                _datestamp(s.utc),
                f"{abs(s.mag_variation_deg):.1f}",
                var_dir,
                "A",  # mode indicator: autonomous
            ),
        )
        return str(msg)

    def vtg(self, s: VesselState) -> str:
        mag_track = _magnetic(s.cog_deg, s.mag_variation_deg)
        msg = pynmea2.VTG(
            self.talker,
            "VTG",
            (
                f"{s.cog_deg:.1f}",  # true track = COG, NOT heading
                "T",
                f"{mag_track:.1f}",
                "M",
                f"{s.sog_kn:.1f}",
                "N",
                f"{s.sog_kn * 1.852:.1f}",
                "K",
                "A",
            ),
        )
        return str(msg)

    def zda(self, s: VesselState) -> str:
        msg = pynmea2.ZDA(
            self.talker,
            "ZDA",
            (
                _timestamp(s.utc),
                f"{s.utc.day:02d}",
                f"{s.utc.month:02d}",
                f"{s.utc.year:04d}",
                "00",
                "00",
            ),
        )
        return str(msg)

    def gll(self, s: VesselState) -> str:
        lat, lat_dir = to_ddmm(s.lat, is_lat=True)
        lon, lon_dir = to_ddmm(s.lon, is_lat=False)
        status = "A" if s.fix_quality > 0 else "V"
        msg = pynmea2.GLL(
            self.talker,
            "GLL",
            (lat, lat_dir, lon, lon_dir, _timestamp(s.utc), status, "A"),
        )
        return str(msg)
