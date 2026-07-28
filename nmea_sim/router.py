"""The AUTO-mode arbiter: per-(input, class) liveness tracking and winner selection.

In ``auto`` mode a single physical input can legitimately feed two output channels (a
satellite compass emits heading sentences for the heading channel and GNSS position/time
for the GPS channel), and each channel names an *ordered* priority list of the inputs it
will accept. The router is the single source of truth that answers two questions the engine
needs on every tick: "which input, if any, is currently the live winner for this channel's
sentence class?" and "given a line that just arrived on an input, which channel should it be
handed to?".

Deliberately, the router holds **no threads and touches no sink**. The engine owns the input
readers, the per-channel inbox queues, and the sinks; the router is pure state plus pure
decisions. Keeping the arbiter thread-free and I/O-free is what lets it be unit-tested entirely
in memory — no serial ports, no pty, just method calls with an injected ``now``. The only shared
mutable state is the liveness map, guarded by a lock so the reader threads writing timestamps and
the worker threads reading them never see a torn value.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .classify import CLASS_TO_ROLE, sentence_class

if TYPE_CHECKING:
    from .config import EngineConfig

# Inverse of CLASS_TO_ROLE: role -> the sentence class it consumes. Roles absent here (instrument)
# consume no live class and so are never suppressed by a winning source.
_CLASS_BY_ROLE: dict[str, str] = {role: cls for cls, role in CLASS_TO_ROLE.items()}


class Router:
    """Single source of truth for per-(input, class) liveness and winner selection.

    Holds NO threads and never touches a sink — the engine owns threads/queues/sinks. This keeps
    the arbiter pure enough to unit-test in memory (no serial, no pty).
    """

    def __init__(self, config: EngineConfig) -> None:
        # role -> channel id, so a sentence class (via CLASS_TO_ROLE) maps to its output channel.
        # Deep validation rejects a config with two channels sharing an arbitrated gps/heading/ais
        # role (validate._validate_cross_channel), so at most one channel per class-bearing role
        # reaches here. This dict-comprehension keeps the LAST channel for a role; that only matters
        # for un-arbitrated roles (e.g. instrument) the router never looks up by class anyway.
        self._channel_by_role: dict[str, str] = {ch.role: ch.id for ch in config.channels}
        # input id -> how long without a valid sentence before that input counts as dead.
        self._timeout_by_input: dict[str, float] = {
            inp.id: inp.liveness_timeout_s for inp in config.inputs
        }
        # channel id -> its ordered source-priority list (highest priority first).
        self._sources_by_channel: dict[str, list[str]] = {
            ch.id: list(ch.sources) for ch in config.channels
        }
        # channel id -> the sentence class its role consumes (None for roles that consume none,
        # e.g. an instrument channel that is never suppressed by a live source).
        self._class_by_channel: dict[str, str | None] = {
            ch.id: _CLASS_BY_ROLE.get(ch.role) for ch in config.channels
        }
        # Liveness store: (input_id, cls) -> monotonic timestamp of the last valid line seen for
        # that pairing. Guarded by a lock; every read/write of the map goes through it so a worker
        # never observes a partially-updated entry while a reader thread is stamping one.
        self._lock = threading.Lock()
        self._liveness: dict[tuple[str, str], float] = {}

    def note_rx(self, input_id: str, line: str, now: float) -> tuple[str, str, str] | None:
        """Classify + route + stamp a line that arrived on ``input_id``.

        Returns ``(target_channel_id, cls, line)`` when the line is a valid source for a real
        channel, else ``None``. Drops (returns ``None``) in three cases: the line does not
        classify (unknown/malformed formatter); no channel exists for the class's role; or this
        input is not listed in that channel's sources (a stray class for this wire). Only a line
        that survives all three checks stamps liveness — so a stray sentence can never make an
        input look live for a channel it isn't a source for.
        """
        cls = sentence_class(line)
        if cls is None:
            return None
        channel_id = self._channel_by_role.get(CLASS_TO_ROLE[cls])
        if channel_id is None:
            return None
        if input_id not in self._sources_by_channel.get(channel_id, ()):
            return None
        with self._lock:
            self._liveness[(input_id, cls)] = now
        return (channel_id, cls, line)

    def winner(self, channel_id: str, cls: str, now: float) -> str | None:
        """Return the highest-priority input that is currently live for this channel+class.

        Walks the channel's sources IN ORDER (highest priority first) and returns the first whose
        last valid line for ``cls`` arrived within that input's ``liveness_timeout_s``. Returns
        ``None`` when no source is live, meaning the channel falls back to generating. Reading the
        whole liveness map under one lock keeps the ordered walk consistent against concurrent
        reader-thread stamps.
        """
        sources = self._sources_by_channel.get(channel_id, ())
        with self._lock:
            for input_id in sources:
                ts = self._liveness.get((input_id, cls))
                if ts is not None and (now - ts) <= self._timeout_by_input.get(input_id, 0.0):
                    return input_id
        return None

    def any_live(self, channel_id: str, cls: str, now: float) -> bool:
        """True iff some source is currently the live winner for this channel+class."""
        return self.winner(channel_id, cls, now) is not None

    def source_label(self, channel_id: str, cls: str, now: float) -> str:
        """Health label for the channel's source: ``"LIVE:<input_id>"`` or ``"SIM"``.

        OFF is not decided here — that is the worker's enable gate; this only distinguishes a live
        passthrough winner from generation.
        """
        winner = self.winner(channel_id, cls, now)
        return f"LIVE:{winner}" if winner is not None else "SIM"

    def live_class_for_input(self, input_id: str, now: float) -> str | None:
        """The sentence class currently live on ``input_id``, or ``None`` if the input is dead.

        A pure read of the liveness store for a *single* input, used by the web layer to report
        per-slot detection without exposing the device path. Walks that input's known (input, class)
        pairings and returns the first whose last valid line arrived within the input's
        ``liveness_timeout_s``. Classes are visited in sorted order so a slot carrying two live
        classes at once (a satellite compass feeding both gnss and heading) reports a stable,
        deterministic one rather than a dict-insertion-order accident. Reads the whole map under the
        one lock so the scan never observes a torn entry against a concurrent reader-thread stamp.
        """
        timeout = self._timeout_by_input.get(input_id, 0.0)
        with self._lock:
            live = [
                cls
                for (inp, cls), ts in self._liveness.items()
                if inp == input_id and (now - ts) <= timeout
            ]
        return min(live) if live else None

    def channel_for_class(self, cls: str) -> str | None:
        """The channel id that consumes ``cls``, or ``None`` if no channel owns that class.

        Inverse of :meth:`channel_class`. The lookup already existed inlined in :meth:`note_rx`;
        it is public here so the provenance resolver can go field -> class -> channel and ask
        :meth:`winner` whether a stored ``live:<input>`` tag is still true.
        """
        role = CLASS_TO_ROLE.get(cls)
        return None if role is None else self._channel_by_role.get(role)

    def channel_class(self, channel_id: str) -> str | None:
        """The sentence class this channel's role consumes, or ``None`` if it consumes none.

        ``gps`` -> ``gnss``, ``heading`` -> ``heading``, ``ais`` -> ``ais``; any other role
        (e.g. an instrument channel) -> ``None``, so it is never suppressed by a live source.
        """
        return self._class_by_channel.get(channel_id)
