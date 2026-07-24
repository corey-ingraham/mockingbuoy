"""AIS sentence generation via ``pyais``.

Encodes own-ship and simulated targets into ``!AIVDO`` / ``!AIVDM`` sentences:

* **Own-ship** uses ``!AIVDO`` (``sentence_type="VDO"``); **targets** use ``!AIVDM``.
* **Class A** contacts transmit **position Type 1** and **static Type 5**; **Class B**
  contacts transmit **position Type 18** and **static Type 24**.
* Multi-fragment messages (Type 5) carry a **sequential message-ID** (0-9) so a receiver
  can reassemble the fragments; single-fragment position reports leave it empty.
* Transmissions **alternate radio channel A/B**, mirroring a real AIS transceiver.
* "Not available" fields use the standard sentinels (heading 511, COG 360°, SOG 102.3 kn,
  ROT -128) — carried on ``AisTarget`` and passed straight through.

``pyais`` computes the six-bit armouring and checksum; this module owns the framing
policy (VDO vs VDM, class→type, channel alternation, seq-id rotation). No CRLF is added.
"""

from __future__ import annotations

from pyais.encode import encode_dict

from .state import AisTarget, VesselState

# The field types ``pyais.encode_dict`` accepts for a message payload.
AisField = str | int | float | bytes | bool


class AisGenerator:
    """Frames own-ship and target state into AIS NMEA sentences.

    Holds two small pieces of cross-call state: the A/B channel toggle and the
    multi-fragment sequential message-ID counter, both of which a real transceiver
    advances monotonically.
    """

    def __init__(self, talker: str = "AI") -> None:
        self.talker = talker
        self._channel_is_a = True
        self._seq_id = 0

    def _next_channel(self) -> str:
        channel = "A" if self._channel_is_a else "B"
        self._channel_is_a = not self._channel_is_a
        return channel

    def _next_seq_id(self) -> int:
        seq = self._seq_id
        self._seq_id = (self._seq_id + 1) % 10
        return seq

    # -- position reports -------------------------------------------------------

    def _position_dict(self, mmsi: int, t: AisTarget) -> dict[str, AisField]:
        """Common position payload for both Class A (Type 1) and Class B (Type 18)."""
        return {
            "mmsi": mmsi,
            "lat": t.lat,
            "lon": t.lon,
            "speed": t.sog_kn,
            "course": t.cog_deg,
            "heading": t.heading_deg,
        }

    def position(self, t: AisTarget, *, own_ship: bool = False) -> list[str]:
        """Build one position report for ``t`` (Type 1 for Class A, Type 18 for Class B)."""
        sentence_type = "VDO" if own_ship else "VDM"
        data = self._position_dict(t.mmsi, t)
        if t.class_type.upper() == "B":
            data["type"] = 18
        else:
            data["type"] = 1
            data["status"] = t.nav_status
            data["turn"] = t.rot
        return encode_dict(
            data,
            talker_id=self.talker,
            sentence_type=sentence_type,
            radio_channel=self._next_channel(),
        )

    def own_ship(self, state: VesselState, mmsi: int, *, class_type: str = "A") -> list[str]:
        """Build the own-ship position report (``!AIVDO``) from a ``VesselState``.

        Own-ship COG/heading come straight from the vessel state, so the AIS view stays
        consistent with the GPS/heading channels driven by the same snapshot.
        """
        target = AisTarget(
            mmsi=mmsi,
            lat=state.lat,
            lon=state.lon,
            sog_kn=state.sog_kn,
            cog_deg=state.cog_deg,
            heading_deg=int(round(state.heading_true_deg)) % 360,
            class_type=class_type,
        )
        return self.position(target, own_ship=True)

    # -- static / voyage reports ------------------------------------------------

    def static(self, t: AisTarget) -> list[str]:
        """Build the static report: Type 5 (Class A) or Type 24 (Class B).

        Multi-fragment (Type 5 spans two sentences) so it consumes a sequential
        message-ID; Type 24 parts A/B are single-fragment each.
        """
        if t.class_type.upper() == "B":
            data_a: dict[str, AisField] = {
                "type": 24,
                "partno": 0,
                "mmsi": t.mmsi,
                "shipname": t.name,
            }
            data_b: dict[str, AisField] = {
                "type": 24,
                "partno": 1,
                "mmsi": t.mmsi,
                "shiptype": t.ship_type,
                "callsign": t.callsign,
            }
            channel = self._next_channel()
            return encode_dict(
                data_a, talker_id=self.talker, sentence_type="VDM", radio_channel=channel
            ) + encode_dict(
                data_b, talker_id=self.talker, sentence_type="VDM", radio_channel=channel
            )
        data: dict[str, AisField] = {
            "type": 5,
            "mmsi": t.mmsi,
            "imo": t.imo,
            "callsign": t.callsign,
            "shipname": t.name,
            "shiptype": t.ship_type,
            "destination": t.destination,
        }
        return encode_dict(
            data,
            talker_id=self.talker,
            sentence_type="VDM",
            radio_channel=self._next_channel(),
            seq_id=self._next_seq_id(),
        )
