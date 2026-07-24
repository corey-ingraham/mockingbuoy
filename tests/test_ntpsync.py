"""NtpSync: the cached, safe-degrading clock-discipline probe.

These tests never touch a real time daemon. Caching is proven by spying on ``_probe`` (which
returns a fixed answer) and asserting it is not re-invoked within the cache window; the
"never raises / degrades to True" contract is proven by the platform's own default probe.
The probe's Linux runtime-path reading is deliberately NOT exercised here — it is the
UNKNOWN-degrades-to-True path that must hold identically on this cross-platform test box.
"""

from __future__ import annotations

import sys

from nmea_sim.ntpsync import NtpSync


def test_synced_caches_within_the_window_and_reprobes_after() -> None:
    """``synced`` probes once, serves the cached answer for ``cache_s`` seconds, then re-probes —
    so the physics tick never forks a probe per call (R27)."""
    ns = NtpSync(cache_s=10.0)
    calls: list[float] = []

    def spy() -> bool:
        calls.append(1.0)
        return True

    ns._probe = spy  # type: ignore[method-assign]

    assert ns.synced(0.0) is True
    assert len(calls) == 1  # first call always probes (never probed before)
    assert ns.synced(5.0) is True
    assert len(calls) == 1  # within the 10 s window -> served from cache, no re-probe
    assert ns.synced(20.0) is True
    assert len(calls) == 2  # window elapsed -> exactly one fresh probe


def test_synced_returns_the_cached_probe_answer() -> None:
    """The cached value tracks whatever the probe last returned (here a forced False)."""
    ns = NtpSync(cache_s=10.0)

    def spy() -> bool:
        return False

    ns._probe = spy  # type: ignore[method-assign]
    assert ns.synced(0.0) is False
    assert ns.synced(1.0) is False  # cached, still False


def test_probe_never_raises_and_returns_a_bool() -> None:
    """The real probe must degrade rather than propagate on any platform — never raises."""
    result = NtpSync()._probe()
    assert isinstance(result, bool)


def test_synced_default_is_true_on_this_platform() -> None:
    """On a non-Linux host (this dev box) there is no timesyncd runtime to confirm an unsynced
    state, so the probe degrades to "assume disciplined"."""
    if sys.platform.startswith("linux"):
        # On Linux the answer legitimately depends on the host's timesyncd state; the degrade-to-
        # True contract for the UNKNOWN case is covered by the non-Linux assertion above.
        return
    assert NtpSync().synced(0.0) is True
