"""AIS generator: decode round-trips, VDO/VDM framing, class-correct types, A/B, seq-ids."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pyais import decode

from nmea_sim.ais_generator import AisGenerator
from nmea_sim.state import AIS_HEADING_NA, AisTarget, VesselState


def _class_a() -> AisTarget:
    return AisTarget(
        mmsi=366000001,
        lat=10.2,
        lon=-30.4,
        sog_kn=10.2,
        cog_deg=95.0,
        heading_deg=280,
        nav_status=0,
        class_type="A",
        ship_type=70,
        name="ALPHA",
        callsign="AAAA",
        destination="PORT",
        imo=1234567,
    )


def _class_b() -> AisTarget:
    return AisTarget(
        mmsi=366000002,
        lat=10.05,
        lon=-30.6,
        sog_kn=5.0,
        cog_deg=100.0,
        heading_deg=AIS_HEADING_NA,
        class_type="B",
        ship_type=37,
        name="BRAVO",
        callsign="BBBB",
    )


def test_class_a_position_is_type1_vdm_and_roundtrips() -> None:
    gen = AisGenerator()
    t = _class_a()
    (sentence,) = gen.position(t)
    assert sentence.startswith("!AIVDM")
    d = decode(sentence)
    assert d.msg_type == 1
    assert d.mmsi == t.mmsi
    assert d.lat == pytest.approx(t.lat, abs=1e-4)
    assert d.lon == pytest.approx(t.lon, abs=1e-4)
    assert d.speed == pytest.approx(t.sog_kn, abs=0.1)
    assert d.course == pytest.approx(t.cog_deg, abs=0.1)
    assert d.heading == t.heading_deg


def test_class_b_position_is_type18() -> None:
    gen = AisGenerator()
    (sentence,) = gen.position(_class_b())
    d = decode(sentence)
    assert d.msg_type == 18


def test_own_ship_uses_vdo() -> None:
    gen = AisGenerator()
    state = _sample_vessel()
    (sentence,) = gen.own_ship(state, mmsi=366999999)
    assert sentence.startswith("!AIVDO")
    d = decode(sentence)
    assert d.mmsi == 366999999
    assert d.course == pytest.approx(state.cog_deg, abs=0.1)


def test_targets_use_vdm_not_vdo() -> None:
    gen = AisGenerator()
    (sentence,) = gen.position(_class_a(), own_ship=False)
    assert sentence.startswith("!AIVDM")


def test_channel_alternates_a_then_b() -> None:
    gen = AisGenerator()
    (first,) = gen.position(_class_a())
    (second,) = gen.position(_class_b())
    # Field 5 of the NMEA sentence is the radio channel.
    assert first.split(",")[4] == "A"
    assert second.split(",")[4] == "B"


def test_class_a_static_is_type5_multifragment() -> None:
    gen = AisGenerator()
    fragments = gen.static(_class_a())
    assert len(fragments) == 2  # Type 5 spans two fragments
    d = decode(*fragments)
    assert d.msg_type == 5
    assert d.shipname.strip() == "ALPHA"
    assert d.callsign.strip() == "AAAA"


def test_static_seq_id_advances_per_multifragment_message() -> None:
    gen = AisGenerator()
    first = gen.static(_class_a())
    second = gen.static(_class_a())
    # The sequential message-ID is field 4 of a multi-fragment sentence.
    assert first[0].split(",")[3] == "0"
    assert second[0].split(",")[3] == "1"


def test_class_b_static_is_type24_parts() -> None:
    gen = AisGenerator()
    fragments = gen.static(_class_b())
    assert len(fragments) == 2  # Type 24 part A + part B
    types = {decode(f).msg_type for f in fragments}
    assert types == {24}


def test_class_a_static_full_roundtrip() -> None:
    """Every static field must survive the wire — the H3 regression: ``ship_type`` (formerly the
    dropped ``shiptype`` key), plus imo/destination/shipname/callsign."""
    gen = AisGenerator()
    t = _class_a()
    d = decode(*gen.static(t))
    assert d.msg_type == 5
    assert d.ship_type == t.ship_type  # was silently 0 under the shiptype-key bug
    assert d.imo == t.imo
    assert d.destination.strip() == t.destination
    assert d.shipname.strip() == t.name
    assert d.callsign.strip() == t.callsign


def test_class_b_static_partb_full_roundtrip() -> None:
    """Type 24 Part B carries ship_type (second H3 call site) + callsign; Part A the name."""
    gen = AisGenerator()
    t = _class_b()
    part_a, part_b = gen.static(t)
    da = decode(part_a)
    db = decode(part_b)
    assert da.msg_type == 24 and da.partno == 0
    assert db.msg_type == 24 and db.partno == 1
    assert db.ship_type == t.ship_type  # was silently 0 under the shiptype-key bug
    assert db.callsign.strip() == t.callsign
    assert da.shipname.strip() == t.name


def test_own_ship_static_uses_vdo() -> None:
    """Own-ship static mirrors own-ship position: it goes out as ``!AIVDO``, not ``!AIVDM``."""
    gen = AisGenerator()
    fragments = gen.static(_class_a(), own_ship=True)
    assert all(f.startswith("!AIVDO") for f in fragments)
    d = decode(*fragments)
    assert d.msg_type == 5
    assert d.ship_type == 70


def test_static_payload_keys_conform_to_pyais_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key handed to ``encode_dict`` must be a real field of the target pyais message.

    ``encode_dict`` silently drops unknown keys, so a misspelling (``shiptype`` vs ``ship_type``)
    zeroes the field on the wire with no error. Asserting payload keys against ``.fields()``
    kills that whole bug class permanently, across every message the generator emits.
    """
    from pyais.encode import MSG_CLASS
    from pyais.messages import MessageType24PartA, MessageType24PartB

    import nmea_sim.ais_generator as agmod

    captured: list[dict[str, Any]] = []
    real_encode = agmod.encode_dict

    def spy(data: dict[str, Any], **kwargs: Any) -> Any:
        captured.append(dict(data))
        return real_encode(data, **kwargs)

    monkeypatch.setattr(agmod, "encode_dict", spy)

    gen = agmod.AisGenerator()
    gen.position(_class_a())
    gen.position(_class_b())
    gen.static(_class_a())
    gen.static(_class_b())
    gen.own_ship(_sample_vessel(), mmsi=366000123)
    gen.static(_class_a(), own_ship=True)

    assert captured
    type24_fields = {f.name for f in MessageType24PartA.fields()} | {
        f.name for f in MessageType24PartB.fields()
    }
    for data in captured:
        msg_type = data["type"]
        valid = type24_fields if msg_type == 24 else {f.name for f in MSG_CLASS[msg_type].fields()}
        unknown = (set(data) - {"type"}) - valid  # 'type' is the encode selector -> msg_type
        assert not unknown, (msg_type, unknown)


def test_heading_sentinel_survives_roundtrip() -> None:
    gen = AisGenerator()
    (sentence,) = gen.position(_class_b())  # class B target has heading NA (511)
    d = decode(sentence)
    assert d.heading == AIS_HEADING_NA


def _sample_vessel() -> VesselState:
    return VesselState(
        lat=10.1,
        lon=-30.5,
        sog_kn=8.0,
        cog_deg=110.0,
        heading_true_deg=115.0,
        heading_mag_deg=118.0,
        mag_variation_deg=-3.0,
        altitude_m=0.0,
        fix_quality=1,
        satellites=10,
        hdop=0.7,
        utc=datetime(2024, 6, 21, 12, 0, 0, tzinfo=UTC),
    )
