"""Heading sentence generation: HDT, HDG, HDM.

These carry **heading** — where the vessel's bow points — which is independent of
course-over-ground (in RMC/VTG). A vessel set by current or wind has a heading that
differs from its COG; the sim keeps them separate on purpose (asserted in tests).

* **HDT** — true heading.
* **HDM** — magnetic heading.
* **HDG** — magnetic heading + deviation + variation (the richest of the three).

Magnetic heading derives from one signed, East-positive variation:
``magnetic = true - variation``. Built with ``pynmea2`` (checksum on ``str()``); no CRLF.
"""

from __future__ import annotations

import pynmea2

from .state import VesselState

SUPPORTED = ("HDT", "HDG", "HDM")


class HeadingGenerator:
    """Builds heading sentences for one talker (default ``HE``) from a ``VesselState``."""

    def __init__(self, talker: str = "HE") -> None:
        self.talker = talker

    def build(self, state: VesselState, sentences: tuple[str, ...] = SUPPORTED) -> list[str]:
        """Return the requested sentences (in order) as strings without CRLF."""
        builders = {"HDT": self.hdt, "HDG": self.hdg, "HDM": self.hdm}
        out: list[str] = []
        for name in sentences:
            try:
                out.append(builders[name](state))
            except KeyError:
                raise ValueError(f"unsupported heading sentence {name!r}") from None
        return out

    def hdt(self, s: VesselState) -> str:
        """True heading."""
        msg = pynmea2.HDT(self.talker, "HDT", (f"{s.heading_true_deg:.1f}", "T"))
        return str(msg)

    def hdm(self, s: VesselState) -> str:
        """Magnetic heading."""
        msg = pynmea2.HDM(self.talker, "HDM", (f"{s.heading_mag_deg:.1f}", "M"))
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
