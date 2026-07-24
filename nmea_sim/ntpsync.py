"""Answer 'is the system clock disciplined?' cheaply, safely, and without ever hard-failing.

The Time Authority consults this from the physics tick to decide whether a real wall-clock
reading deserves the ``"ntp"`` tag (disciplined) or the ``"system"`` tag (free-running). That
question is only asked when NO GNSS source is live, so it is a fallback signal — but it is still
on the hot path, which drives two hard rules:

* It MUST NOT fork a subprocess per call (R27). Shelling out to ``chronyc``/``timedatectl``/
  ``adjtimex`` every tick would be both slow and a hang risk, so the answer is cached for a few
  seconds and the underlying probe is deliberately file-only — no process is ever spawned.
* It MUST NOT raise or block. Every probe is wrapped so that any missing file, permission error,
  or unexpected platform quirk degrades to "assume disciplined" rather than propagating.

The probe is intentionally conservative: it returns ``False`` **only** when it can positively
confirm the clock is unsynchronised, and otherwise degrades to ``True``. Concretely, on Linux we
lean on ``systemd-timesyncd``'s runtime marker: the daemon creates
``/run/systemd/timesync/synchronized`` once the clock is disciplined and its runtime directory
``/run/systemd/timesync`` exists whenever the daemon is active. So a present marker means synced;
a running daemon (directory present) with the marker still absent is a *confirmed* unsynced state;
anything else — a different time daemon, no timesyncd at all, a non-Linux host (this Windows dev
box included), or any error reading the paths — is UNKNOWN and degrades to ``True``. A separate
plausibility guard (year >= 2020) is layered on by the caller, so this need only speak to sync
status, not to whether the resulting timestamp is sane.
"""

from __future__ import annotations

import os
import sys

# systemd-timesyncd's runtime footprint. The marker file exists iff the clock is disciplined; the
# directory exists whenever the daemon is running. Their combination is what lets us distinguish a
# *confirmed* unsynced state (daemon up, marker absent) from a merely UNKNOWN one (no daemon here).
_TIMESYNC_DIR = "/run/systemd/timesync"
_TIMESYNC_MARKER = "/run/systemd/timesync/synchronized"


class NtpSync:
    """Cached, safe-degrading probe of whether the system clock is disciplined.

    Consulted from the physics tick, so ``synced`` returns a value cached for ``cache_s`` seconds
    and never spawns a process, blocks, or raises. See the module docstring for the probe's
    conservative "confirm-unsynced-or-assume-True" contract.
    """

    def __init__(self, cache_s: float = 10.0) -> None:
        self._cache_s = cache_s
        # ``_probed_at`` is a ``time.monotonic()`` stamp; ``None`` means "never probed", forcing the
        # first ``synced`` call to probe. Cached default is ``True`` so we err toward disciplined.
        self._probed_at: float | None = None
        self._cached = True

    def synced(self, now_monotonic: float) -> bool:
        """Return whether the clock is disciplined, cached for ``cache_s`` seconds.

        ``now_monotonic`` is a caller-supplied ``time.monotonic()`` reading — passed in rather than
        read here so the physics tick controls the clock and the method stays trivially testable.
        """
        if self._probed_at is not None and now_monotonic - self._probed_at < self._cache_s:
            return self._cached
        self._cached = self._probe()
        self._probed_at = now_monotonic
        return self._cached

    def _probe(self) -> bool:
        """Best-effort, dependency-free, exception-swallowing sync check. Never raises.

        Returns ``False`` only on a positively confirmed unsynced state; degrades to ``True`` for
        every UNKNOWN outcome (non-Linux, no timesyncd, any error touching the runtime paths).
        """
        # Non-Linux hosts have no systemd-timesyncd runtime dir to consult; there is nothing to
        # confirm, so degrade to "assume disciplined" without touching the filesystem at all.
        if not sys.platform.startswith("linux"):
            return True
        try:
            if os.path.exists(_TIMESYNC_MARKER):
                return True  # daemon has flagged the clock disciplined
            if os.path.isdir(_TIMESYNC_DIR):
                # Daemon is running (its runtime dir exists) yet the marker is absent: this is the
                # one case we can positively call unsynchronised.
                return False
        except OSError:
            # Any filesystem hiccup is UNKNOWN, not a confirmed failure — degrade to True.
            return True
        # No timesyncd footprint at all: some other (or no) time daemon. UNKNOWN -> assume synced.
        return True
