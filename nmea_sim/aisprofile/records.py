"""The one record shape both ingestion sources yield.

``csv_source`` (tabular exports) and ``aivdm_source`` (decoded NMEA captures) both emit an
``AisRecord``; ``profile`` aggregates a stream of them into the statistics-only profile dict
that :class:`nmea_sim.realism.RealismProfile` already understands. The record is a *transient*
carrier — it is aggregated and discarded, never persisted — so no individual identity leaves
the pipeline; only the distilled statistics do.

Two internal sentinels let the two sources share one shape without a second schema:

* ``ship_type == _SHIP_TYPE_UNKNOWN`` (``-1``) — the source reported no ship type for this
  record (an AIS position report carries none; only a static report does). The aggregator
  simply does not update the vessel's category from such a record.
* ``lat``/``lon``/``sog``/``cog`` may be ``nan`` — the source reported no position for this
  record (an AIS static report carries none). The aggregator skips ``nan`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# A ship type this record does not report (position reports carry none; static reports do).
_SHIP_TYPE_UNKNOWN = -1


@dataclass(frozen=True)
class AisRecord:
    """A single AIS observation, normalised across both ingestion sources.

    Angles are degrees, speed is knots, ``lat``/``lon`` are WGS-84 degrees. ``ts`` is the
    observation time when the source carries one (tabular exports do; raw AIS position/static
    reports do not, so it is ``None`` there). ``transceiver_class`` is ``"A"``/``"B"`` (or ``""``
    when unknown). See the module docstring for the ``nan`` / ``-1`` "not reported" sentinels.
    """

    mmsi: int
    ts: datetime | None
    lat: float
    lon: float
    sog: float
    cog: float
    ship_type: int
    transceiver_class: str
