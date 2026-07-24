"""Shared, thread-safe vessel state — the single source of truth all senders read.

``VesselState`` is a frozen dataclass: a snapshot is immutable, so a sender can format
sentences from it *after* releasing the lock (never hold a lock across ``serial.write``).
``SharedState`` guards the current snapshot with one lock and swaps it atomically via
``dataclasses.replace``.

Correctness invariant enforced across the codebase and asserted in tests:
**course-over-ground (``cog_deg``) and heading (``heading_*_deg``) are independent.**
RMC/VTG consume COG; HDT/HDG/HDM consume heading. They are never cross-wired.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

# AIS "not available" sentinels (used by the P2 AIS generator; defined here with the
# target model so the values live in one place).
AIS_HEADING_NA = 511
AIS_COG_NA = 3600  # tenths of a degree
AIS_SOG_NA = 1023  # tenths of a knot
AIS_ROT_NA = -128
AIS_LAT_NA = 91 * 600000  # 1/10000 min
AIS_LON_NA = 181 * 600000


@dataclass(frozen=True)
class VesselState:
    """Immutable own-ship snapshot. All angles in degrees, speed in knots."""

    lat: float
    lon: float
    sog_kn: float
    cog_deg: float
    heading_true_deg: float
    heading_mag_deg: float
    mag_variation_deg: float  # signed, East-positive (True = Magnetic + Variation_east)
    altitude_m: float
    fix_quality: int  # GGA quality indicator: 0=no fix, 1=GPS, 2=DGPS, ...
    satellites: int
    hdop: float
    utc: datetime


@dataclass(frozen=True)
class AisTarget:
    """A single AIS contact (own-ship or simulated target). Fleshed out in P2."""

    mmsi: int
    lat: float
    lon: float
    sog_kn: float = 0.0
    cog_deg: float = 0.0
    heading_deg: int = AIS_HEADING_NA
    nav_status: int = 15  # 15 = "not defined"
    rot: int = AIS_ROT_NA
    class_type: str = "A"  # "A" -> Type 1/2/3, "B" -> Type 18
    ship_type: int = 0
    name: str = ""
    callsign: str = ""
    destination: str = ""
    imo: int = 0


class SharedState:
    """Thread-safe holder of the current ``VesselState`` (one lock, atomic swap)."""

    def __init__(self, initial: VesselState) -> None:
        self._lock = threading.Lock()
        self._state = initial

    def snapshot(self) -> VesselState:
        """Return the current immutable snapshot (safe to use after the lock releases)."""
        with self._lock:
            return self._state

    def update(self, **changes: Any) -> VesselState:
        """Atomically replace fields on the current state; return the new snapshot."""
        with self._lock:
            self._state = replace(self._state, **changes)
            return self._state
