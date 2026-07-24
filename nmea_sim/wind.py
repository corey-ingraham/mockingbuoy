"""Apparent-wind vector arithmetic for NMEA wind sentences.

A single pure helper that resolves the apparent (relative) wind a moving vessel
experiences, given the true wind and the vessel's motion over ground. The result
is expressed as a bow-relative angle (0 = wind from dead ahead, 90 = from the
starboard beam) and a speed, ready for an ``MWV``-style sentence.

The maths is a plain vector subtraction in the earth frame:

* The **true wind** is reported meteorologically as a *from* bearing, so its
  velocity vector points *toward* ``true_dir_deg + 180``.
* The **vessel velocity** vector is speed-over-ground along course-over-ground.
  Heading is deliberately *not* used here — a vessel set by current moves along
  its COG, not where its bow points.
* The **apparent wind** velocity is the true-wind velocity minus the vessel
  velocity. Its magnitude is the apparent speed; the bearing it blows *from*
  (opposite the velocity vector), rotated into the vessel's frame by subtracting
  heading, is the bow-relative angle.
"""

from __future__ import annotations

import math


def apparent_wind(
    true_speed_kn: float,
    true_dir_deg: float,
    heading_deg: float,
    cog_deg: float,
    sog_kn: float,
) -> tuple[float, float]:
    """Resolve apparent wind from true wind and vessel motion over ground.

    Return ``(apparent_speed_kn, apparent_angle_deg)`` where ``apparent_angle_deg``
    is normalized to ``[0, 360)`` and measured relative to the bow (0 = wind from
    dead ahead, 90 = from the starboard beam).
    """
    # True-wind velocity vector: a meteorological FROM bearing points TOWARD +180.
    wind_toward = math.radians(true_dir_deg + 180.0)
    wind_n = true_speed_kn * math.cos(wind_toward)
    wind_e = true_speed_kn * math.sin(wind_toward)

    # Vessel velocity vector: speed over ground along course over ground (not heading).
    cog = math.radians(cog_deg)
    vessel_n = sog_kn * math.cos(cog)
    vessel_e = sog_kn * math.sin(cog)

    # Apparent-wind velocity vector in the earth frame.
    apparent_n = wind_n - vessel_n
    apparent_e = wind_e - vessel_e

    apparent_speed = math.hypot(apparent_n, apparent_e)

    # Bearing the apparent wind blows FROM (opposite its velocity vector), then
    # rotated into the vessel frame so 0 is dead ahead.
    from_bearing = math.degrees(math.atan2(-apparent_e, -apparent_n))
    apparent_angle = (from_bearing - heading_deg) % 360.0

    return apparent_speed, apparent_angle
