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
# target model so the values live in one place). These are in the ENGINEERING units
# ``pyais`` expects on encode — NOT raw six-bit wire units — so they can be handed
# straight to ``encode_dict`` (pyais applies the ×10 / ×600000 scaling itself). Passing
# raw wire units here instead makes pyais wrap them into plausible garbage (e.g. a raw
# SOG of 1023 encodes as 101.4 kn), so keep these engineering-valued.
AIS_HEADING_NA = 511  # degrees
AIS_COG_NA = 360.0  # degrees
AIS_SOG_NA = 102.3  # knots
AIS_ROT_NA = -128  # ROT special "not available"
AIS_LAT_NA = 91.0  # degrees
AIS_LON_NA = 181.0  # degrees


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
    # New fields MUST stay after ``utc`` (which has no default): a frozen dataclass cannot
    # place a non-default field after a defaulted one. Each carries its own default.
    stw_kn: float = 0.0  # speed through water, knots
    depth_m: float = 0.0  # depth below transducer, metres
    rot_dpm: float = 0.0  # rate of turn, deg/min, + = starboard
    wind_speed_kn: float = 0.0  # TRUE wind speed, knots
    wind_dir_deg: float = 0.0  # TRUE wind direction, deg true, FROM
    sea_state: int = 1  # WMO sea state 0-9, drives pitch/roll motion
    pitch_deg: float = 0.0  # + = bow up (derived by physics)
    roll_deg: float = 0.0  # + = starboard down (derived by physics)
    rudder_angle_deg: float = 0.0  # + = starboard (for RSA, later phase)
    set_deg: float = 0.0  # current set, deg true (for VDR, later phase)
    drift_kn: float = 0.0  # current drift, knots (for VDR, later phase)


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
