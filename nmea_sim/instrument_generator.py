"""Instrument sentence generation: VHW, DPT, DBT, MWV, MWD, ROT, XDR, RSA, VDR, PASHR.

These carry the "everything else" a modern helm sees: water speed and depth
(VHW/DPT/DBT), wind (MWV apparent, MWD true), turn rate (ROT), attitude
(XDR pitch/roll, PASHR the proprietary attitude packet), rudder (RSA), and the
current set/drift the vessel is being pushed by (VDR).

Wind splits along the same axis the rest of the codebase keeps sacred:

* **MWV** reports *apparent* wind — what the masthead vane actually feels — so it
  is resolved from true wind and vessel motion via ``wind.apparent_wind`` (which
  uses COG/SOG, never heading, for the vessel-velocity term).
* **MWD** reports *true* wind — the meteorological FROM direction and speed —
  straight off the state, with the magnetic direction derived from the one signed,
  East-positive variation: ``magnetic = true - variation``.

Every standard sentence is built with ``pynmea2`` (which appends the checksum on
``str()``); PASHR has no reliable pynmea2 class, so it is hand-built and closed with
the shared ``checksum`` helper. Nothing here bakes in a line ending — the serial
layer appends ``\\r\\n``.
"""

from __future__ import annotations

import pynmea2

from . import checksum
from .state import VesselState
from .wind import apparent_wind

# Sentences this generator knows how to build.
SUPPORTED = ("VHW", "DPT", "DBT", "MWV", "MWD", "ROT", "XDR", "RSA", "VDR", "PASHR")

# Unit conversion constants.
_KMH_PER_KN = 1.852  # knots -> km/h
_M_PER_FOOT = 0.3048  # feet -> metres (feet = m / this)
_M_PER_FATHOM = 1.8288  # fathoms -> metres (fathoms = m / this)
_MS_PER_KN = 0.514444  # knots -> m/s


def _magnetic(true_deg: float, variation_deg: float) -> float:
    """Magnetic bearing from a true bearing and East-positive variation."""
    return (true_deg - variation_deg) % 360.0


class InstrumentGenerator:
    """Builds instrument sentences for one talker (default ``II``) from a ``VesselState``."""

    # Sentence name -> builder method name. Hoisted to the class so the dispatch table is
    # built once at import, not rebuilt on every emission (this runs at each sentence's rate).
    _BUILDERS = {
        "VHW": "vhw",
        "DPT": "dpt",
        "DBT": "dbt",
        "MWV": "mwv",
        "MWD": "mwd",
        "ROT": "rot",
        "XDR": "xdr",
        "RSA": "rsa",
        "VDR": "vdr",
        "PASHR": "pashr",
    }

    def __init__(self, talker: str = "II") -> None:
        self.talker = talker

    def build(self, state: VesselState, sentences: tuple[str, ...] = SUPPORTED) -> list[str]:
        """Return the requested sentences (in order) as strings without CRLF."""
        out: list[str] = []
        for name in sentences:
            try:
                builder = getattr(self, self._BUILDERS[name])
            except KeyError:
                raise ValueError(f"unsupported instrument sentence {name!r}") from None
            out.append(builder(state))
        return out

    def vhw(self, s: VesselState) -> str:
        """Water speed and heading: true/magnetic heading, speed in knots and km/h."""
        msg = pynmea2.VHW(
            self.talker,
            "VHW",
            (
                f"{s.heading_true_deg:.1f}",
                "T",
                f"{s.heading_mag_deg:.1f}",
                "M",
                f"{s.stw_kn:.1f}",
                "N",
                f"{s.stw_kn * _KMH_PER_KN:.1f}",
                "K",
            ),
        )
        return str(msg)

    def dpt(self, s: VesselState) -> str:
        """Depth below transducer with a zero offset (referenced to the transducer).

        The offset field distinguishes depth-below-keel (negative offset) from
        depth-below-waterline (positive offset); ``0.0`` means the reported depth is
        referenced straight to the transducer face, with no pin-0 correction applied.
        """
        msg = pynmea2.DPT(self.talker, "DPT", (f"{s.depth_m:.1f}", "0.0"))
        return str(msg)

    def dbt(self, s: VesselState) -> str:
        """Depth below transducer expressed three ways: feet, metres, fathoms."""
        feet = s.depth_m / _M_PER_FOOT
        fathoms = s.depth_m / _M_PER_FATHOM
        msg = pynmea2.DBT(
            self.talker,
            "DBT",
            (
                f"{feet:.1f}",
                "f",
                f"{s.depth_m:.1f}",
                "M",
                f"{fathoms:.1f}",
                "F",
            ),
        )
        return str(msg)

    def mwv(self, s: VesselState) -> str:
        """Apparent wind: bow-relative angle and speed resolved from true wind + motion."""
        app_speed, app_angle = apparent_wind(
            s.wind_speed_kn,
            s.wind_dir_deg,
            s.heading_true_deg,
            s.cog_deg,
            s.sog_kn,
        )
        msg = pynmea2.MWV(
            self.talker,
            "MWV",
            (
                f"{app_angle:.1f}",
                "R",  # reference: R = relative (apparent), T = theoretical (true)
                f"{app_speed:.1f}",
                "N",
                "A",  # status: A = valid
            ),
        )
        return str(msg)

    def mwd(self, s: VesselState) -> str:
        """True wind: FROM direction (true + magnetic) and speed (knots + m/s)."""
        dir_mag = _magnetic(s.wind_dir_deg, s.mag_variation_deg)
        msg = pynmea2.MWD(
            self.talker,
            "MWD",
            (
                f"{s.wind_dir_deg:.1f}",
                "T",
                f"{dir_mag:.1f}",
                "M",
                f"{s.wind_speed_kn:.1f}",
                "N",
                f"{s.wind_speed_kn * _MS_PER_KN:.1f}",
                "M",
            ),
        )
        return str(msg)

    def rot(self, s: VesselState) -> str:
        """Rate of turn in degrees/minute (+ = starboard), status valid."""
        msg = pynmea2.ROT(self.talker, "ROT", (f"{s.rot_dpm:.1f}", "A"))
        return str(msg)

    def xdr(self, s: VesselState) -> str:
        """Two angular transducers in one sentence: pitch (PTCH) then roll (ROLL).

        Each group is ``(type='A', value, units='D', name)`` per the Airmar
        convention. Sign convention matches the state model: ``+pitch = bow up``,
        ``+roll = starboard down``.
        """
        msg = pynmea2.XDR(
            self.talker,
            "XDR",
            (
                "A",
                f"{s.pitch_deg:.1f}",
                "D",
                "PTCH",
                "A",
                f"{s.roll_deg:.1f}",
                "D",
                "ROLL",
            ),
        )
        return str(msg)

    def rsa(self, s: VesselState) -> str:
        """Rudder sensor angle (+ = starboard). Single-rudder: starboard populated, port empty."""
        msg = pynmea2.RSA(
            self.talker,
            "RSA",
            (
                f"{s.rudder_angle_deg:.1f}",
                "A",
                "",  # port rudder angle: absent on a single-rudder vessel
                "",  # port status
            ),
        )
        return str(msg)

    def vdr(self, s: VesselState) -> str:
        """Current set and drift: set direction (true + magnetic) and drift in knots."""
        set_mag = _magnetic(s.set_deg, s.mag_variation_deg)
        msg = pynmea2.VDR(
            self.talker,
            "VDR",
            (
                f"{s.set_deg:.1f}",
                "T",
                f"{set_mag:.1f}",
                "M",
                f"{s.drift_kn:.1f}",
                "N",
            ),
        )
        return str(msg)

    def pashr(self, s: VesselState) -> str:
        """Proprietary attitude sentence, hand-built (no reliable pynmea2 class).

        Field layout (a widely-used inertial/attitude form) — eleven fields after the
        ``$PASHR`` address::

            $PASHR,hhmmss.sss,HHH.HH,T,RRR.RR,PPP.PP,heave,rr.rrr,pp.ppp,hh.hhh,qG,qI*CS
                    |          |     | |      |      |     |       |       |     |  |
                    time       heading true  roll   pitch heave  roll-acc pitch  hdg  GPS  INS
                                     flag                        (m)      acc     acc  qual status

        This sim populates time, true heading, roll, pitch, and a zero heave; the
        accuracy tail and the two trailing quality flags (GPS update quality, INS
        status) are fixed plausible values. The body is closed with the shared XOR
        checksum so the line is a valid ``$PASHR,...*HH``.
        """
        hhmmss = f"{s.utc:%H%M%S}.{s.utc.microsecond // 1000:03d}"
        body = (
            f"PASHR,{hhmmss},"
            f"{s.heading_true_deg:.2f},T,"
            f"{s.roll_deg:.2f},"
            f"{s.pitch_deg:.2f},"
            "0.00,"  # heave, metres
            "0.000,0.000,0.000,"  # roll / pitch / heading accuracy, degrees
            "2,"  # GPS update quality flag (2 = RTK-fixed-class)
            "0"  # INS status flag
        )
        return checksum.format_sentence(body)
