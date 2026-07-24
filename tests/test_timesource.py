"""TimeAuthority priority resolution, driven deterministically with stubbed collaborators.

No serial, no real clock discipline, no sleeps: a stub Router hands the authority a fixed
GNSS winner, a stub base clock returns a controlled UTC per tick, and an injected NtpSync
answers the sync question, so every priority tier (gps/sat/ntp/system/simulated/hold) is
exercised by feeding fixes directly and reading back the resolved tag. ``advance`` reads
``time.monotonic`` internally, so projection is asserted as a *window* (seconds elapsed since
a fix's injected arrival) rather than an exact instant — the only non-determinism, bounded.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from nmea_sim.timeauthority import TimeAuthority

_INPUT_TAG = {"gps_in": "gps", "sat_in": "sat"}


class _StubRouter:
    """A Router surface that always names one winner — the only method TimeAuthority calls."""

    def __init__(self, winner: str | None) -> None:
        self._winner = winner

    def winner(self, channel_id: str, cls: str, now: float) -> str | None:
        return self._winner


class _StubClock:
    """A base clock returning a fixed UTC per tick, with a settable ``mode`` (the clock surface)."""

    def __init__(self, mode: str, out: datetime) -> None:
        self.mode = mode
        self._out = out

    def advance(self, current: datetime, dt_s: float) -> datetime:
        return self._out


class _StubNtp:
    """An NtpSync surface with a fixed ``synced`` answer — no probe, no cache to exercise here."""

    def __init__(self, synced: bool) -> None:
        self._synced = synced

    def synced(self, now_monotonic: float) -> bool:
        return self._synced


def _authority(*, winner: str | None, base: _StubClock, ntp: _StubNtp) -> TimeAuthority:
    return TimeAuthority(base, _StubRouter(winner), "gps", dict(_INPUT_TAG), ntp)  # type: ignore[arg-type]


def test_source_tag_defaults_to_system_before_first_advance() -> None:
    auth = _authority(
        winner=None,
        base=_StubClock("system_utc", datetime(2026, 1, 1, tzinfo=UTC)),
        ntp=_StubNtp(True),
    )
    assert auth.source_tag() == "system"


def test_gps_fix_wins_over_ntp_and_projects_forward() -> None:
    """A live GPS fix beats an otherwise-synced NTP clock (tag "gps") and its UTC ticks forward
    by the monotonic time elapsed since the fix arrived — proving time rides the position winner."""
    fix_utc = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    auth = _authority(
        # The base clock is a synced wall clock; it must lose to the live GNSS fix.
        winner="gps_in",
        base=_StubClock("system_utc", datetime(2026, 6, 1, tzinfo=UTC)),
        ntp=_StubNtp(True),
    )
    # Stamp the fix as having arrived 10 s ago (in monotonic terms), so advance projects it forward.
    auth.note_time("gps_in", fix_utc, time.monotonic() - 10.0)
    resolved = auth.advance(fix_utc, 0.05)
    assert auth.source_tag() == "gps"
    # Projected forward by the ~10 s of monotonic elapsed since the fix arrived (bounded window).
    elapsed = resolved - fix_utc
    assert timedelta(seconds=10.0) <= elapsed < timedelta(seconds=11.0)


def test_monotonic_clamp_never_steps_backward_across_handover() -> None:
    """A fresh GPS fix that resolves *behind* the current clock is clamped up to it — a source
    handover must never step the real-time clock backward (R7/R51)."""
    fix_utc = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    auth = _authority(
        winner="gps_in",
        base=_StubClock("system_utc", datetime(2026, 1, 1, tzinfo=UTC)),
        ntp=_StubNtp(True),
    )
    auth.note_time("gps_in", fix_utc, time.monotonic())
    # current is an hour ahead of the fix's projection, so the clamp must pin the result to current.
    current = fix_utc + timedelta(hours=1)
    resolved = auth.advance(current, 0.05)
    assert auth.source_tag() == "gps"
    assert resolved == current


def test_sat_wins_when_gps_input_is_not_the_live_winner() -> None:
    """When the SAT input is the router's winner (GPS not live), time comes from it, tag "sat"."""
    fix_utc = datetime(2025, 3, 3, 4, 5, 6, tzinfo=UTC)
    auth = _authority(
        winner="sat_in",
        base=_StubClock("system_utc", datetime(2026, 1, 1, tzinfo=UTC)),
        ntp=_StubNtp(True),
    )
    auth.note_time("sat_in", fix_utc, time.monotonic())
    resolved = auth.advance(fix_utc - timedelta(hours=1), 0.05)
    assert auth.source_tag() == "sat"
    assert resolved >= fix_utc


def test_no_winner_system_utc_tags_ntp_when_synced_and_plausible() -> None:
    base_utc = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    auth = _authority(winner=None, base=_StubClock("system_utc", base_utc), ntp=_StubNtp(True))
    resolved = auth.advance(datetime(2026, 5, 5, 11, 0, 0, tzinfo=UTC), 0.05)
    assert auth.source_tag() == "ntp"
    assert resolved == base_utc


def test_no_winner_system_utc_tags_system_when_probe_reports_unsynced() -> None:
    base_utc = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    auth = _authority(winner=None, base=_StubClock("system_utc", base_utc), ntp=_StubNtp(False))
    auth.advance(datetime(2026, 5, 5, 11, 0, 0, tzinfo=UTC), 0.05)
    # Probe confirms the clock is not disciplined -> the free-running "system" tier, never "ntp".
    assert auth.source_tag() == "system"


def test_no_winner_system_utc_tags_system_when_year_implausible() -> None:
    """Even a "synced" probe cannot promote an implausible (pre-2020) wall clock to "ntp"."""
    base_utc = datetime(1999, 1, 1, tzinfo=UTC)
    auth = _authority(winner=None, base=_StubClock("system_utc", base_utc), ntp=_StubNtp(True))
    auth.advance(datetime(1999, 1, 1, tzinfo=UTC), 0.05)
    assert auth.source_tag() == "system"


def test_simulated_base_is_honoured_verbatim_and_never_clamped() -> None:
    """A scripted "simulated" clock is passed straight through — clamping it up to ``current``
    would break its configured drift, so a value behind ``current`` must survive unclamped."""
    base_utc = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)
    auth = _authority(winner=None, base=_StubClock("simulated", base_utc), ntp=_StubNtp(True))
    # current is far ahead of the demo clock; a real-time clamp would drag base_utc up to it.
    resolved = auth.advance(datetime(2030, 1, 1, tzinfo=UTC), 0.05)
    assert auth.source_tag() == "simulated"
    assert resolved == base_utc  # NOT clamped up to current


def test_hold_base_is_honoured_verbatim_and_never_clamped() -> None:
    base_utc = datetime(2000, 6, 1, 0, 0, 0, tzinfo=UTC)
    auth = _authority(winner=None, base=_StubClock("hold", base_utc), ntp=_StubNtp(True))
    resolved = auth.advance(datetime(2030, 1, 1, tzinfo=UTC), 0.05)
    assert auth.source_tag() == "hold"
    assert resolved == base_utc


def test_winner_live_but_no_fix_yet_falls_through_without_raising() -> None:
    """The router names a live winner but no time has been parsed for it yet: advance must fall
    through to the base clock instead of crashing on a missing fix."""
    base_utc = datetime(2026, 2, 2, 2, 2, 2, tzinfo=UTC)
    auth = _authority(winner="gps_in", base=_StubClock("system_utc", base_utc), ntp=_StubNtp(True))
    resolved = auth.advance(datetime(2026, 2, 2, 1, 0, 0, tzinfo=UTC), 0.05)
    # No fix stored -> base tier resolves; never the "gps" tier, and never an exception.
    assert auth.source_tag() in ("ntp", "system")
    assert resolved == base_utc


def test_note_time_ignores_inputs_absent_from_input_tag() -> None:
    """A time-bearing line on a non-GNSS input can never seed the clock: note_time drops it, so a
    winner pointing at that id still finds no fix and falls through to the base clock."""
    base_utc = datetime(2026, 7, 7, tzinfo=UTC)
    auth = _authority(
        winner="stray_in", base=_StubClock("system_utc", base_utc), ntp=_StubNtp(True)
    )
    auth.note_time("stray_in", datetime(2025, 1, 1, tzinfo=UTC), time.monotonic())
    resolved = auth.advance(datetime(2026, 7, 6, tzinfo=UTC), 0.05)
    assert auth.source_tag() in ("ntp", "system")
    assert resolved == base_utc
