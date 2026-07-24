"""Smoke test: the package imports and exposes a version string."""

import nmea_sim


def test_version_present() -> None:
    assert isinstance(nmea_sim.__version__, str)
    assert nmea_sim.__version__
