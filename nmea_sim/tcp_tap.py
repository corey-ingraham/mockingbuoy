"""Per-channel raw NMEA-over-TCP tap: a read-only broadcaster.

Each tapped channel exposes a plain TCP listener that mirrors exactly the lines written to
its serial port. Standard marine tools (OpenCPN, chart plotters, ``nc``) subscribe to it.
It is a ``Writer`` sink like any other, so the engine fans emitted lines to it with the same
per-sink isolation as serial/log/web.

Security-relevant invariants (see ``docs/ref/security.md``):

* **Read-only.** Bytes a client sends are never read — there is no inbound path into the
  sim, so a tap cannot inject state or commands.
* **Bind to an explicit host** (a LAN IP in production), never ``0.0.0.0``. The host is a
  required constructor argument; there is no wildcard default.
* **Drop-oldest per client.** Each client has a bounded buffer; a slow or stalled consumer
  loses its own oldest lines but never stalls the broadcaster or other clients.
* **Bounded subscriber count.** At most ``max_clients`` connections are served at once; further
  connections are accepted and immediately closed, so an unauthenticated flood of taps cannot
  exhaust threads/memory on a small (GIL-bound) host.
* **Non-blocking sends.** Each client socket has a send timeout and TCP keep-alive, so a stalled
  reader is reaped instead of pinning a sender thread on a blocking ``sendall`` forever.

Threads use composition (``threading.Thread(target=...)``) to avoid shadowing ``Thread``
internals.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections import deque

# Per-client line buffer bound. When exceeded, the oldest queued line is discarded.
_DEFAULT_MAX_QUEUE = 2000
# Max simultaneous subscribers. Beyond this, new connections are accepted then dropped so an
# unauthenticated tap flood cannot exhaust threads/memory (matches the listen backlog).
_DEFAULT_MAX_CLIENTS = 8
# Per-send socket timeout (seconds). A stalled reader trips this and its sender thread reaps the
# client, instead of blocking forever in ``sendall`` with data queuing behind it.
_SEND_TIMEOUT_S = 5.0


class _Client:
    """One connected subscriber: a bounded outbound buffer drained by its own thread."""

    def __init__(self, sock: socket.socket, max_queue: int) -> None:
        self.sock = sock
        self._buf: deque[bytes] = deque(maxlen=max_queue)
        self._cond = threading.Condition()
        self.dropped = 0
        self.alive = True
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def enqueue(self, data: bytes) -> None:
        with self._cond:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1  # deque drops the oldest automatically on append
            self._buf.append(data)
            self._cond.notify()

    def _send_loop(self) -> None:
        while True:
            with self._cond:
                while self.alive and not self._buf:
                    self._cond.wait()
                if not self.alive:
                    return
                data = self._buf.popleft()
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False
                return

    def close(self) -> None:
        with self._cond:
            self.alive = False
            self._cond.notify()
        with contextlib.suppress(OSError):
            self.sock.close()


class TcpTap:
    """A read-only NMEA-over-TCP broadcaster bound to a specific host:port."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        max_clients: int = _DEFAULT_MAX_CLIENTS,
    ) -> None:
        if not host or host == "0.0.0.0":  # noqa: S104 - explicitly forbidding the wildcard
            raise ValueError("TcpTap requires an explicit bind host (never 0.0.0.0)")
        self._host = host
        self._port = port
        self._max_queue = max_queue
        self._max_clients = max_clients
        self._server: socket.socket | None = None
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._acceptor: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        """The actual listening port (useful when constructed with port 0 in tests)."""
        if self._server is None:
            return self._port
        return self._server.getsockname()[1]

    def client_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._clients if c.alive)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._host, self._port))
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self._acceptor = threading.Thread(
            target=self._accept_loop, name=f"tap-{self._port}", daemon=True
        )
        self._acceptor.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                break
            try:
                sock, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(_SEND_TIMEOUT_S)  # a stalled reader trips this, reaping the client
            with self._lock:
                live = self._prune_dead()
                if len(live) >= self._max_clients:
                    # At capacity: refuse the connection so a tap flood can't exhaust the host.
                    with contextlib.suppress(OSError):
                        sock.close()
                    continue
                self._clients.append(_Client(sock, self._max_queue))

    def _prune_dead(self) -> list[_Client]:
        """Drop dead clients, closing each one's socket (caller must hold ``self._lock``).

        Pruning without closing would leak the accepted server-side socket until GC — so
        each dropped client is closed here. Returns the surviving live clients.
        """
        live: list[_Client] = []
        for client in self._clients:
            if client.alive:
                live.append(client)
            else:
                client.close()
        self._clients = live
        return live

    # -- Writer protocol ----------------------------------------------------
    def write_line(self, line: str) -> None:
        """Broadcast ``line`` + CRLF to every live client (drop-oldest on slow ones)."""
        data = (line + "\r\n").encode("ascii", "replace")
        with self._lock:
            for client in self._prune_dead():
                client.enqueue(data)

    def close(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            with contextlib.suppress(OSError):
                server.close()
        self._server = None
        acceptor = self._acceptor
        if acceptor is not None and acceptor.is_alive():
            acceptor.join(2.0)
        with self._lock:
            for client in self._clients:
                client.close()
            self._clients = []
