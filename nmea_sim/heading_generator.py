"""Heading sentence generation: HDT, HDG, HDM, THS.

These carry **heading** — where the vessel's bow points — which is independent of
course-over-ground (in RMC/VTG). A vessel set by current or wind has a heading that
differs from its COG; the sim keeps them separate on purpose (asserted in tests).

* **HDT** — true heading.
* **HDM** — magnetic heading.
* **HDG** — magnetic heading + deviation + variation (the richest of the three).
* **THS** — true heading + mode status (the modern replacement for HDT).

Magnetic heading derives from one signed, East-positive variation:
``magnetic = true - variation``. Built with ``pynmea2`` (checksum on ``str()``); no CRLF.
"""

from __future__ import annotations

import pynmea2

from .state import VesselState


class THS(pynmea2.TalkerSentence):
    """True heading + mode status (NMEA 0183 THS); pynmea2 ships no class for it.

    Defining this subclass registers it with pynmea2's sentence metaclass, so the
    sentence both builds via ``str()`` and round-trips through ``pynmea2.parse``.
    Field layout ``$--THS,x.x,a*hh``: true heading then a single-char mode indicator.
    """

    fields = (
        ("True Heading", "heading", float),
        ("Mode Indicator", "mode_indicator"),
    )


SUPPORTED = ("HDT", "HDG", "HDM", "THS")


class HeadingGenerator:
    """Builds heading sentences for one talker (default ``HE``) from a ``VesselState``."""

    # Sentence name -> builder method name. Hoisted to the class so the dispatch table is
    # built once at import, not rebuilt on every emission (this runs at each sentence's rate).
    _BUILDERS = {"HDT": "hdt", "HDG": "hdg", "HDM": "hdm", "THS": "ths"}

    def __init__(self, talker: str = "HE") -> None:
        self.talker = talker

    def build(self, state: VesselState, sentences: tuple[str, ...] = SUPPORTED) -> list[str]:
        """Return the requested sentences (in order) as strings without CRLF."""
        out: list[str] = []
        for name in sentences:
            try:
                builder = getattr(self, self._BUILDERS[name])
            except KeyError:
                raise ValueError(f"unsupported heading sentence {name!r}") from None
            out.append(builder(state))
        return out

    def hdt(self, s: VesselState) -> str:
        """True heading."""
        msg = pynmea2.HDT(self.talker, "HDT", (f"{s.heading_true_deg:.1f}", "T"))
        return str(msg)

    def hdm(self, s: VesselState) -> str:
        """Magnetic heading."""
        msg = pynmea2.HDM(self.talker, "HDM", (f"{s.heading_mag_deg:.1f}", "M"))
        return str(msg)

    def ths(self, s: VesselState) -> str:
        """True heading with mode status (``S`` = simulator, the standard self-identification)."""
        msg = THS(self.talker, "THS", (f"{s.heading_true_deg:.1f}", "S"))
        return str(msg)

    def hdg(self, s: VesselState) -> str:
        """Magnetic heading with (zero) deviation and the signed variation."""
        var_dir = "E" if s.mag_variation_deg >= 0 else "W"
        msg = pynmea2.HDG(
            self.talker,
            "HDG",
            (
                f"{s.heading_mag_deg:.1f}",
                "0.0",  # deviation (sim assumes a compensated compass)
                "E",
                f"{abs(s.mag_variation_deg):.1f}",
                var_dir,
            ),
        )
        return str(msg)
