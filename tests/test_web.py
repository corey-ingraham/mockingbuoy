"""Web layer tests: FastAPI TestClient (context-manager form) + a deterministic Broker unit test.

Covers the HTTP/SSE contract: index page, auto-start + healthz transitions, config/state JSON,
update validation (409/400), SSE framing, Broker drop-oldest, import layering, and clean shutdown.

TestClient MUST be used as a context manager so the app lifespan (which auto-starts the engine and
starts the pump + health-broadcast tasks) actually runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import janus
import pytest
from fastapi.testclient import TestClient

import web.app as web_app
from web.app import Broker, SubscriberLimitError, create_app

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient bound to the deterministic repo config (writer_backend 'log', no hardware).

    Entering the context runs lifespan startup (engine auto-start); exiting runs shutdown
    (engine stop + task cancel), so each test is isolated.
    """
    app = create_app(str(CONFIG_PATH))
    with TestClient(app) as test_client:
        yield test_client


# --- index -------------------------------------------------------------------------


def test_index_serves_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<html" in resp.text.lower()


# --- auto-start + healthz transitions ----------------------------------------------


def test_autostart_and_healthz_transitions(client: TestClient) -> None:
    # Engine auto-starts on lifespan startup -> healthy.
    assert client.get("/healthz").status_code == 200

    stopped = client.post("/api/control", json={"action": "stop"})
    assert stopped.status_code == 200
    assert stopped.json()["running"] is False

    # Stopped -> 503.
    assert client.get("/healthz").status_code == 503

    started = client.post("/api/control", json={"action": "start"})
    assert started.status_code == 200
    assert started.json()["running"] is True

    assert client.get("/healthz").status_code == 200


# --- config ------------------------------------------------------------------------


def test_config_endpoint(client: TestClient) -> None:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "channels" in body
    assert isinstance(body["channels"], list)


# --- state + update ----------------------------------------------------------------


def test_update_state_reflected_in_snapshot(client: TestClient) -> None:
    new_lat = 12.3456
    new_lon = 56.7890
    resp = client.post("/api/control", json={"action": "update", "lat": new_lat, "lon": new_lon})
    assert resp.status_code == 200
    assert resp.json()["running"] is True

    state = client.get("/api/state")
    assert state.status_code == 200
    body = state.json()
    # Movement mode is 'static', so the updated lat does not drift.
    assert body["lat"] == pytest.approx(new_lat)
    assert body["lon"] == pytest.approx(new_lon)


def test_update_while_stopped_conflicts(client: TestClient) -> None:
    assert client.post("/api/control", json={"action": "stop"}).status_code == 200
    resp = client.post("/api/control", json={"action": "update", "lat": 10.0})
    assert resp.status_code == 409


def test_state_endpoint_stopped_branch(client: TestClient) -> None:
    """``GET /api/state`` returns 200 with a minimal ``{"running": False}`` when stopped."""
    assert client.post("/api/control", json={"action": "stop"}).status_code == 200
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert resp.json() == {"running": False}


def test_unknown_action_is_bad_request(client: TestClient) -> None:
    resp = client.post("/api/control", json={"action": "frobnicate"})
    assert resp.status_code == 400


def test_out_of_range_update_is_bad_request(client: TestClient) -> None:
    resp = client.post("/api/control", json={"action": "update", "lat": 999.0})
    assert resp.status_code == 400


# --- SSE ---------------------------------------------------------------------------


def test_sse_stream_emits_events() -> None:
    """Exercise ``GET /api/stream`` end-to-end and assert framing + at least one event.

    Starlette's *sync* ``TestClient`` runs the ASGI app to completion and buffers the whole
    body before returning, so it deadlocks on an infinite SSE stream (and httpx's
    ``ASGITransport`` buffers too). We therefore drive the ASGI app directly with a
    controlled ``receive``/``send`` pair, capturing the first frames and then issuing an
    ``http.disconnect`` so the endpoint's own disconnect handling tears the stream down.
    The whole scenario is bounded by ``asyncio.wait_for`` so it can never hang. This still
    goes through real routing, the auth dependency, ``StreamingResponse``, and the Broker
    fan-out, and relies on the guaranteed ~1s ``health`` frame.
    """

    async def scenario() -> None:
        app = create_app(str(CONFIG_PATH))
        async with app.router.lifespan_context(app):
            captured: dict[str, Any] = {"status": None, "headers": {}, "body": b""}
            got = asyncio.Event()
            disconnect = asyncio.Event()

            async def receive() -> dict[str, Any]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: Any) -> None:
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                    captured["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
                elif message["type"] == "http.response.body":
                    captured["body"] += message.get("body", b"")
                    if b"event:" in captured["body"]:
                        got.set()

            scope: dict[str, Any] = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/stream",
                "raw_path": b"/api/stream",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 80),
            }

            task = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(got.wait(), timeout=5.0)
            finally:
                disconnect.set()
                await asyncio.wait_for(task, timeout=5.0)

            assert captured["status"] == 200
            headers = captured["headers"]
            assert headers["content-type"].startswith("text/event-stream")
            assert headers["cache-control"] == "no-cache"
            assert headers["x-accel-buffering"] == "no"
            body = captured["body"]
            assert b"event: health" in body or b"event: nmea" in body

    asyncio.run(asyncio.wait_for(scenario(), timeout=15.0))


# --- Broker drop-oldest (deterministic unit test) ----------------------------------


def test_broker_drop_oldest_on_overflow() -> None:
    async def scenario() -> None:
        broker = Broker()
        # The janus.Queue must be constructed inside the running loop (as lifespan does).
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=10_000)
        broker.bind(queue)
        sub = broker.subscribe()
        try:
            cap = sub.maxsize
            assert cap > 0, "subscriber queue must be bounded"

            pump_task = asyncio.create_task(broker.pump())
            try:
                overflow = 5
                total = cap + overflow
                for i in range(total):
                    broker.publish("ch", f"line-{i}")
                    # Let the pump drain the ingress queue so drops happen at the bounded
                    # per-subscriber queue (what we are testing), not at ingress.
                    await asyncio.sleep(0)

                # Bounded poll for the janus sync->async bridge to settle, instead of a
                # fixed sleep: wait until the subscriber queue has drained to exactly `cap`.
                deadline = asyncio.get_event_loop().time() + 2.0
                while sub.qsize() != cap and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.01)

                drained: list[str] = []
                while not sub.empty():
                    drained.append(str(sub.get_nowait()))
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task

            # Bounded queue kept only the newest `cap` items; oldest were dropped.
            assert len(drained) == cap
            joined = "\n".join(drained)
            assert "line-0" not in joined
            assert f"line-{total - 1}" in joined

        finally:
            broker.unsubscribe(sub)
            broker.close()

    asyncio.run(scenario())


# --- optional in-app Basic auth: fail closed on half-config ------------------------


def test_half_configured_basic_auth_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting exactly one of the two Basic-auth env vars must raise, not silently disable."""
    monkeypatch.setenv("MOCKINGBUOY_BASIC_USER", "operator")
    monkeypatch.delenv("MOCKINGBUOY_BASIC_HASH", raising=False)
    with pytest.raises(RuntimeError, match="half-configured"):
        create_app(str(CONFIG_PATH))

    # The symmetric case (hash without user) also fails closed.
    monkeypatch.delenv("MOCKINGBUOY_BASIC_USER", raising=False)
    monkeypatch.setenv("MOCKINGBUOY_BASIC_HASH", "$argon2id$dummy")
    with pytest.raises(RuntimeError, match="half-configured"):
        create_app(str(CONFIG_PATH))


def test_basic_auth_verify_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both Basic-auth env vars are set, credentials are actually checked.

    No creds -> 401 with ``WWW-Authenticate: Basic``; correct user/pass -> 200; wrong
    password -> 401. Skips if the passlib argon2 backend is unavailable in this env.
    """
    passlib_context = pytest.importorskip("passlib.context")
    pwd_context = passlib_context.CryptContext(schemes=["argon2"], deprecated="auto")
    try:
        password_hash = pwd_context.hash("s3cret-pass")
    except Exception as exc:  # pragma: no cover - environment without argon2 backend
        pytest.skip(f"argon2 backend unavailable: {exc!r}")

    monkeypatch.setenv("MOCKINGBUOY_BASIC_USER", "operator")
    monkeypatch.setenv("MOCKINGBUOY_BASIC_HASH", password_hash)

    app = create_app(str(CONFIG_PATH))
    with TestClient(app) as authed_client:
        no_creds = authed_client.get("/healthz")
        assert no_creds.status_code == 401
        assert no_creds.headers["www-authenticate"].lower().startswith("basic")

        ok = authed_client.get("/healthz", auth=("operator", "s3cret-pass"))
        assert ok.status_code == 200

        wrong = authed_client.get("/healthz", auth=("operator", "not-the-password"))
        assert wrong.status_code == 401


# --- SSE subscriber cap ------------------------------------------------------------


def test_subscriber_cap_rejects_over_limit() -> None:
    async def scenario() -> None:
        broker = Broker()
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=10_000)
        broker.bind(queue)
        subs = []
        try:
            # Fill to the cap, then the next subscribe must be refused.
            from web.app import _MAX_SUBSCRIBERS

            for _ in range(_MAX_SUBSCRIBERS):
                subs.append(broker.subscribe())
            with pytest.raises(SubscriberLimitError):
                broker.subscribe()
        finally:
            for sub in subs:
                broker.unsubscribe(sub)
            broker.close()

    asyncio.run(scenario())


def test_broker_unsubscribe_leaves_no_leaked_subscriber() -> None:
    """subscribe() then unsubscribe() must return the Broker's subscriber set to empty.

    Exercises the same teardown path the SSE stream generator's ``finally`` clause drives
    on client disconnect, at the Broker level (deterministic, no ASGI plumbing needed).
    """

    async def scenario() -> None:
        broker = Broker()
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=10_000)
        broker.bind(queue)

        sub = broker.subscribe()
        assert len(broker._subscribers) == 1
        broker.unsubscribe(sub)
        assert len(broker._subscribers) == 0

        # Idempotent: unsubscribing an already-removed (or unknown) queue is a no-op.
        broker.unsubscribe(sub)
        assert len(broker._subscribers) == 0
        broker.close()

    asyncio.run(scenario())


# --- import layering ---------------------------------------------------------------


def test_no_tkinter_imported() -> None:
    import ast
    import inspect
    import sys

    import nmea_sim.engine as engine_module

    assert "tkinter" not in sys.modules

    # The dependency arrow is one-way (web -> nmea_sim): the pure engine layer must never
    # import the web framework or its ASGI server. Checked statically so it is meaningful
    # even though ``web.app`` is already imported elsewhere in this test module.
    tree = ast.parse(inspect.getsource(engine_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])
    assert "web" not in imported_names
    assert "uvicorn" not in imported_names
    assert web_app is not None  # sanity: the web module itself still imports fine


# --- clean shutdown (no leaked engine threads) -------------------------------------


def test_clean_shutdown_leaves_no_engine_threads() -> None:
    with TestClient(create_app(str(CONFIG_PATH))) as c:
        assert c.get("/healthz").status_code == 200

    lingering = [
        t.name
        for t in threading.enumerate()
        if t.name == "physics" or t.name.startswith("channel-")
    ]
    assert lingering == [], f"engine threads leaked after shutdown: {lingering}"
