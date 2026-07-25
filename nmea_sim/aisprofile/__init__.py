"""Build a statistics-only realism profile from AIS data (CSV export or NMEA capture).

Turns historical AIS into the region-neutral profile dict that
:class:`nmea_sim.realism.RealismProfile` already consumes, so synthetic traffic can be *shaped*
to resemble a sampled area without ever re-broadcasting an identifiable vessel: only aggregate
statistics (bounding box, type mix, speed distributions, concurrent count, Class-A fraction)
leave the pipeline. Runs on the standard library plus ``pyais``, with no runtime internet
dependency.

Two sources yield a common :class:`AisRecord`, which :func:`build_profile` aggregates:

* :mod:`nmea_sim.aisprofile.csv_source` — a Marine-Cadastre-style tabular export.
* :mod:`nmea_sim.aisprofile.aivdm_source` — a captured ``!AIVDM``/``!AIVDO`` NMEA log.
"""

from __future__ import annotations

from .profile import build_profile
from .records import AisRecord

__all__ = ["AisRecord", "build_profile"]
