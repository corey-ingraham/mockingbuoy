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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .classify import CLASS_TO_ROLE, sentence_class

if TYPE_CHECKING:
    from .config import EngineConfig

# Inverse of CLASS_TO_ROLE: role -> the sentence class it consumes. Roles absent here (instrument)
# consume no live class and so are never suppressed by a winning source.
_CLASS_BY_ROLE: dict[str, str] = {role: cls for cls, role in CLASS_TO_ROLE.items()}


@dataclass(frozen=True)
class RxDecision:
    """Where one received line goes, and whether it counts as proof the source is alive.

    Splitting those two questions is the whole reason this type exists. ``note_rx`` used to return a
    bare ``(channel_id, cls, line)`` tuple and stamp liveness for every line it routed, which
    conflated "forward this" with "this source is live". Once unclassified traffic is forwarded too
    (``ChannelSpec.rx_transparent_relay``), that conflation would let a talker's status/alarm
    chatter hold a channel LIVE forever and suppress generation — the exact opposite of what a
    simulate-on-signal-loss rig needs.

    * ``kind == "arbitrated"`` — a classified line for a channel listing this input in ``sources``.
      Liveness IS stamped (by ``note_rx``, before returning) and the worker re-checks it is still
      the winner before forwarding, so a lower-priority source cannot talk over a live one.
    * ``kind == "transparent"`` — a line the channel would otherwise drop. Liveness is NOT stamped.
      The worker forwards it only while the channel is already LIVE, so it disappears on fallback.

    ``cls`` is the sentence class for an arbitrated decision and ``None`` for a transparent one (an
    unclassified line has no class at all) — which is why this cannot stay a 3-tuple of ``str``.
    """

    kind: Literal["arbitrated", "transparent"]
    channel_id: str
    cls: str | None
    line: str
    # The input this line arrived on. Carried here rather than alongside so the worker's inbox holds
    # ONE self-describing object; the arbitrated path needs it to re-check it is still the winner.
    input_id: str


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
        # input id -> the channel that transparently relays what it would otherwise drop. Built once
        # here so the RX hot path stays a dict lookup instead of a scan over channels per line. Deep
        # validation rejects an input listed by two transparent-relay channels (the target would be
        # config-order dependent and silent), so at most one channel per input reaches here.
        self._transparent_by_input: dict[str, str] = {
            src: ch.id for ch in config.channels if ch.rx_transparent_relay for src in ch.sources
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

    def note_rx(self, input_id: str, line: str, now: float) -> RxDecision | None:
        """Classify + route a line from ``input_id``, stamping liveness only when arbitrated.

        Returns an ``arbitrated`` :class:`RxDecision` — and stamps ``(input_id, cls)`` liveness —
        when the line classifies, a channel owns that class's role, and this input is listed in that
        channel's ``sources``. Only a line surviving all three checks stamps liveness, so a stray
        sentence can never make an input look live for a channel it isn't a source for.

        Otherwise, if some channel transparently relays this input, returns a ``transparent``
        decision WITHOUT stamping liveness. That covers both an unclassified line (status/alarm,
        vendor ``$P...``) and a classified-but-unroutable one (say a GNSS sentence arriving on an
        AIS wire, whose role's channel does not list this input) — in both cases the channel would
        otherwise have dropped it. Returning early on the arbitrated path is what stops one line
        being delivered twice.

        Returns ``None`` when neither applies: nothing to forward, nothing to stamp.
        """
        cls = sentence_class(line)
        if cls is not None:
            channel_id = self._channel_by_role.get(CLASS_TO_ROLE[cls])
            if channel_id is not None and input_id in self._sources_by_channel.get(channel_id, ()):
                with self._lock:
                    self._liveness[(input_id, cls)] = now
                return RxDecision("arbitrated", channel_id, cls, line, input_id)
        transparent_id = self._transparent_by_input.get(input_id)
        if transparent_id is not None:
            return RxDecision("transparent", transparent_id, None, line, input_id)
        return None

    def clear_liveness(self, input_id: str) -> None:
        """Forget every liveness stamp for ``input_id``, so it counts as dead immediately.

        Called when an input slot is MUTED (``Engine.set_input_enabled``). Muting already stops new
        lines reaching the router, but the stamps it already made keep it winning until they age out
        — so a mute used to take ``liveness_timeout_s`` to show up as a fallback. Dropping the
        stamps here makes the transition immediate and deterministic, which is what makes the mute
        usable as a signal-loss test instrument instead of a timeout-tuning exercise.

        Unmuting needs no counterpart: the next valid line re-stamps liveness by itself.
        """
        with self._lock:
            for key in [k for k in self._liveness if k[0] == input_id]:
                del self._liveness[key]

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
