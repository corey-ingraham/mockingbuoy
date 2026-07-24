"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nmea_sim.state import VesselState


@pytest.fixture
def sample_state() -> VesselState:
    """A deterministic, geographically-neutral vessel state for generator tests.

    COG (95) and heading (280) are deliberately far apart so tests can prove the two
    are never cross-wired.
    """
    return VesselState(
        lat=25.12345,
        lon=-80.54321,
        sog_kn=12.3,
        cog_deg=95.0,
        heading_true_deg=280.0,
        heading_mag_deg=283.0,
        mag_variation_deg=-3.0,  # West variation
        altitude_m=15.4,
        fix_quality=1,
        satellites=9,
        hdop=0.8,
        utc=datetime(2024, 6, 21, 12, 35, 19, 420000, tzinfo=UTC),
    )
