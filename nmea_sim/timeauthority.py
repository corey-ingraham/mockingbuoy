"""The AUTO-mode Time Authority: one source feeds both position and time, or neither does.

On a real bus the danger is a *split brain* — position coming from the GNSS winner while time
drifts in from some other reading — because a divergent time/position pair is worse than either
being briefly stale. This arbiter closes that gap by binding the clock to the SAME winner the
router already picked for the GPS output channel: whichever input is the live GNSS winner supplies
the authoritative UTC, so time and position can never come from two different sources.

It wraps the plain ``TimeSource`` and is dropped into ``PhysicsEngine`` in place of it (auto mode
only), overriding the base clock ONLY while a GNSS source is live. When no source is live the base
clock is honoured verbatim (R8): ``system_utc`` becomes the real-wall-clock fallback tier
(NTP-disciplined vs free-running system), while ``simulated``/``hold`` are passed straight through
so a scripted demo clock behaves exactly as configured.

Two invariants shape ``advance``:

* Single-source, immutable fixes (R7). Each source's last parsed UTC is held as a frozen ``_Fix``
  record swapped atomically under a lock, so the reader thread stamping a new fix and the physics
  thread projecting one never see a torn value.
* Monotonic clamp on real-time tiers only (R7/R51). The gps/sat/ntp/system tiers all track wall
  time, so across a source handover we never step the clock backward — a slightly-behind new source
  is clamped up to the current value. The ``simulated``/``hold`` tiers are NEVER clamped: they are
  meant to be driftable/freezable, and clamping them would break or freeze the demo clock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Typing-only: these are used purely in annotations, and importing them under TYPE_CHECKING
    # keeps the module import-light and cycle-proof (the engine imports this module, so pulling
    # TimeSource back from .engine at runtime would form a cycle).
    from .ntpsync import NtpSync
    from .router import Router


class _Clock(Protocol):
    """The narrow clock surface TimeAuthority wraps — structurally satisfied by ``TimeSource``.

    Typed as a Protocol rather than importing ``TimeSource`` directly so this module never depends
    on ``.engine`` (which imports it): TimeAuthority only needs a base clock that can ``advance``
    and report its ``mode``, so any object with that shape is accepted (a duck-typed clock).
    """

    mode: str

    def advance(self, current: datetime, dt_s: float) -> datetime: ...


@dataclass(frozen=True)
class _Fix:
    """An immutable snapshot of one source's most recent parsed UTC.

    Frozen so a fix can be swapped in atomically (assign a new record; never mutate a live one),
    letting the physics thread project a fix forward while a reader thread installs the next one
    without any half-written state being observable.
    """

    utc: datetime  # last parsed UTC from that source
    arrival: float  # time.monotonic() at parse time, so it can be projected forward between fixes
    tag: str  # "gps" or "sat" — the source tier that produced this fix


class TimeAuthority:
    """Resolve the authoritative UTC + a source tag each tick, unified with the GNSS position pick.

    A drop-in for ``TimeSource`` (same ``advance`` signature) so ``PhysicsEngine`` is unchanged. It
    overrides the wrapped base clock only while a GNSS source is live; otherwise the base clock is
    honoured per its mode (R8). Fixes are immutable ``_Fix`` records swapped atomically under a lock
    (R7), and the last resolved tag is published for another thread to read via ``source_tag``.
    """

    def __init__(
        self,
        base: _Clock,
        router: Router,
        gps_channel_id: str,
        input_tag: dict[str, str],
        ntp: NtpSync,
    ) -> None:
        self._base = base
        self._router = router
        self._gps_channel_id = gps_channel_id
        # input id -> its tier tag ("gps" or "sat"), derived from InputSpec.function upstream. Only
        # ids present here may store a fix; note_time ignores everything else (non-GNSS inputs).
        self._input_tag = dict(input_tag)
        self._ntp = ntp
        # input id -> its latest immutable fix. Guarded by the lock together with _last_tag so the
        # ordered "winner has a fix?" read and the tag publish are consistent against concurrent
        # reader-thread stamps.
        self._lock = threading.Lock()
        self._fixes: dict[str, _Fix] = {}
        # Last resolved source tag, read by source_tag() from another thread. Defaults to "system"
        # so a caller reading before the first advance() gets a sane free-running-clock label.
        self._last_tag = "system"

    def note_time(self, input_id: str, utc: datetime, now: float) -> None:
        """Record a freshly parsed UTC for ``input_id`` as an immutable fix, atomically.

        Called from the RX dispatch path when ``rx.parse_time`` succeeds. Ignores any input not in
        ``input_tag`` (i.e. non-GNSS inputs) so a stray time-bearing line on the wrong wire can
        never become a time source.
        """
        tag = self._input_tag.get(input_id)
        if tag is None:
            return
        fix = _Fix(utc=utc, arrival=now, tag=tag)
        with self._lock:
            self._fixes[input_id] = fix

    def advance(self, current: datetime, dt_s: float) -> datetime:
        """Resolve the authoritative UTC for this tick — GNSS winner if live, else the base clock.

        Drop-in for ``TimeSource.advance``. When the router reports a live GNSS winner AND we hold a
        fix for it, project that fix forward from its arrival to now (so the clock keeps ticking
        between sentences) and clamp it monotonically against ``current``. When there is no live
        winner — or a winner is live but no fix has been parsed for it yet — fall through to the
        base clock, honouring its mode.
        """
        now = time.monotonic()
        winner = self._router.winner(self._gps_channel_id, "gnss", now)
        # Snapshot the winner's fix under the lock; the immutable record is safe to use unlocked.
        fix = None
        if winner is not None:
            with self._lock:
                fix = self._fixes.get(winner)

        if fix is not None:
            # Live GNSS source with a parsed fix: project forward and clamp to real-time monotonic.
            projected = fix.utc + timedelta(seconds=now - fix.arrival)
            resolved, tag = projected, fix.tag
            # Real-time tier: never step backward across a source handover (R7/R51).
            resolved = max(resolved, current)
        else:
            # No live GNSS fix — either no winner, or a winner is live but has produced no parsable
            # time yet. DO NOT crash; fall through to the base clock and honour its mode (R8).
            base_utc = self._base.advance(current, dt_s)
            if self._base.mode == "system_utc":
                # Real wall clock: NTP-disciplined vs free-running system, with a plausibility guard
                # so a wildly-off clock (pre-2020) is never labelled "ntp".
                if self._ntp.synced(now) and base_utc.year >= 2020:
                    resolved, tag = base_utc, "ntp"
                else:
                    resolved, tag = base_utc, "system"
                # Real-time tier: clamp monotonic.
                resolved = max(resolved, current)
            else:
                # simulated / hold: honoured verbatim and NEVER clamped (R51) — clamping a scripted
                # or held demo clock would freeze it or break its configured drift.
                resolved, tag = base_utc, self._base.mode

        # Publish the resolved tag for source_tag(); a plain str assignment is atomic under the GIL.
        self._last_tag = tag
        return resolved

    def source_tag(self) -> str:
        """The tag of the last resolved tier ("gps"/"sat"/"ntp"/"system"/"simulated"/"hold").

        Readable from another thread (e.g. health surfacing); returns "system" before the first
        ``advance`` call.
        """
        return self._last_tag
