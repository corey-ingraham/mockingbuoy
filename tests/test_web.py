"""Web layer tests: FastAPI TestClient (context-manager form) + a deterministic Broker unit test.

Covers the HTTP/SSE contract: index page, auto-start + healthz transitions, config/state JSON,
update validation (409/400), SSE framing, Broker drop-oldest, import layering, and clean shutdown.

TestClient MUST be used as a context manager so the app lifespan (which auto-starts the engine and
starts the pump + health-broadcast tasks) actually runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
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


# --- per-channel enable/disable ----------------------------------------------------


def _channel_entry(client: TestClient, channel_id: str) -> dict[str, Any]:
    """Pull one channel's dict out of the ``/healthz`` payload (the same body the 1 Hz
    health broadcast fans out to SSE clients, so asserting here covers both)."""
    body = client.get("/healthz").json()
    return next(c for c in body["channels"] if c["channel_id"] == channel_id)


def test_health_payload_carries_enabled_per_channel(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["channels"]
    # Every channel reports the flag, and the shipped config leaves them all on.
    assert all("enabled" in c for c in body["channels"])
    assert all(c["enabled"] is True for c in body["channels"])


def test_channel_toggle_disables_then_re_enables(client: TestClient) -> None:
    off = client.post(
        "/api/control", json={"action": "channel", "channel_id": "gps", "enabled": False}
    )
    assert off.status_code == 200
    assert off.json() == {"running": True, "channel_id": "gps", "enabled": False}

    assert _channel_entry(client, "gps")["enabled"] is False
    # Only the named channel moves; the rest of the config is untouched.
    others = [c for c in client.get("/healthz").json()["channels"] if c["channel_id"] != "gps"]
    assert others and all(c["enabled"] is True for c in others)

    on = client.post(
        "/api/control", json={"action": "channel", "channel_id": "gps", "enabled": True}
    )
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert _channel_entry(client, "gps")["enabled"] is True


def test_muted_channel_does_not_break_healthz(client: TestClient) -> None:
    """Muting is not a fault: ``/healthz`` must stay 200 with the channel still alive."""
    assert (
        client.post(
            "/api/control", json={"action": "channel", "channel_id": "ais", "enabled": False}
        ).status_code
        == 200
    )
    resp = client.get("/healthz")
    assert resp.status_code == 200
    ais = next(c for c in resp.json()["channels"] if c["channel_id"] == "ais")
    assert ais["enabled"] is False
    assert ais["alive"] is True


def test_channel_toggle_unknown_id_is_not_found(client: TestClient) -> None:
    resp = client.post(
        "/api/control", json={"action": "channel", "channel_id": "nope", "enabled": False}
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"action": "channel", "enabled": False},  # no channel_id
        {"action": "channel", "channel_id": "gps"},  # no enabled
        {"action": "channel"},  # neither
    ],
)
def test_channel_toggle_requires_both_fields(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/api/control", json=body).status_code == 400


def test_channel_toggle_while_stopped_conflicts(client: TestClient) -> None:
    """Same guard as ``update``: there is no worker to flag when the engine is stopped."""
    assert client.post("/api/control", json={"action": "stop"}).status_code == 200
    resp = client.post(
        "/api/control", json={"action": "channel", "channel_id": "gps", "enabled": False}
    )
    assert resp.status_code == 409


def test_channel_fields_do_not_leak_into_state_update() -> None:
    """``state_changes()`` walks ``_UPDATE_RANGES`` only, so the two field groups sharing
    one request model cannot cross-contaminate — a channel toggle yields no state edit."""
    from web.app import ControlRequest

    toggle = ControlRequest(action="channel", channel_id="gps", enabled=False)
    assert toggle.state_changes() == {}

    update = ControlRequest(action="update", lat=1.5)
    assert update.state_changes() == {"lat": 1.5}
    assert update.channel_id is None
    assert update.enabled is None


def test_health_to_dict_includes_enabled() -> None:
    """Unit-level check on the serializer itself, independent of a running engine."""
    from nmea_sim.engine import ChannelHealth, HealthReport
    from web.app import _health_to_dict

    report = HealthReport(
        ok=True,
        physics_alive=True,
        channels=[
            ChannelHealth(
                channel_id="gps",
                alive=True,
                emitted=3,
                build_errors=0,
                sinks=[],
                last_emit_age_s=0.25,
                enabled=False,
                source="OFF",
            )
        ],
    )
    channel = _health_to_dict(report)["channels"][0]
    assert channel["enabled"] is False
    assert channel["alive"] is True  # alive and enabled are reported independently


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
    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_USER", "operator")
    monkeypatch.delenv("MOCKINGBUOY_APP_BASIC_HASH", raising=False)
    with pytest.raises(RuntimeError, match="half-configured"):
        create_app(str(CONFIG_PATH))

    # The symmetric case (hash without user) also fails closed.
    monkeypatch.delenv("MOCKINGBUOY_APP_BASIC_USER", raising=False)
    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_HASH", "$argon2id$dummy")
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

    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_USER", "operator")
    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_HASH", password_hash)

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


# --- Phase C: new manual state fields (update + serialization) ---------------------


def test_manual_fields_update_and_reflect_in_state(client: TestClient) -> None:
    """The newly-added MANUAL instrument/nav fields apply via ``update`` and reflect on read.

    ``sea_state`` is posted as a float and applied as-is (the engine clamps/rounds it to the WMO
    integer scale); the rest round-trip verbatim in ``static`` movement mode (no drift)."""
    edits = {
        "action": "update",
        "stw_kn": 7.5,
        "depth_m": 42.0,
        "rot_dpm": 30.0,
        "wind_speed_kn": 15.0,
        "wind_dir_deg": 210.0,
        "sea_state": 4.0,
        "rudder_angle_deg": -12.5,
        "set_deg": 175.0,
        "drift_kn": 1.25,
    }
    resp = client.post("/api/control", json=edits)
    assert resp.status_code == 200
    assert resp.json()["running"] is True

    body = client.get("/api/state").json()
    assert body["stw_kn"] == pytest.approx(7.5)
    assert body["depth_m"] == pytest.approx(42.0)
    assert body["rot_dpm"] == pytest.approx(30.0)
    assert body["wind_speed_kn"] == pytest.approx(15.0)
    assert body["wind_dir_deg"] == pytest.approx(210.0)
    assert body["sea_state"] == pytest.approx(4)  # int/float agnostic
    assert body["rudder_angle_deg"] == pytest.approx(-12.5)
    assert body["set_deg"] == pytest.approx(175.0)
    assert body["drift_kn"] == pytest.approx(1.25)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sea_state", 12.0),  # above WMO max 9
        ("depth_m", 99999.0),  # above 12000 m
        ("rudder_angle_deg", 80.0),  # above the web ±45 bound
        ("wind_speed_kn", 500.0),  # above 200 kn
    ],
)
def test_out_of_range_manual_update_is_bad_request(
    client: TestClient, field: str, value: float
) -> None:
    resp = client.post("/api/control", json={"action": "update", field: value})
    assert resp.status_code == 400


def test_state_to_dict_includes_new_fields_and_apparent_wind(sample_state: Any) -> None:
    """``_state_to_dict`` carries every new manual + derived field, and the apparent-wind pair
    it computes ON READ matches ``nmea_sim.wind.apparent_wind`` for a known state."""
    from nmea_sim.wind import apparent_wind
    from web.app import _state_to_dict

    d = _state_to_dict(sample_state)

    for key in (
        "stw_kn",
        "depth_m",
        "rot_dpm",
        "wind_speed_kn",
        "wind_dir_deg",
        "sea_state",
        "pitch_deg",
        "roll_deg",
        "rudder_angle_deg",
        "set_deg",
        "drift_kn",
        "app_wind_speed_kn",
        "app_wind_angle_deg",
    ):
        assert key in d, f"missing {key!r} in _state_to_dict output"

    exp_speed, exp_angle = apparent_wind(
        sample_state.wind_speed_kn,
        sample_state.wind_dir_deg,
        sample_state.heading_true_deg,
        sample_state.cog_deg,
        sample_state.sog_kn,
    )
    assert d["app_wind_speed_kn"] == pytest.approx(exp_speed)
    assert d["app_wind_angle_deg"] == pytest.approx(exp_angle)
    # ``utc`` stays ISO 8601 (round-trips through fromisoformat).
    assert d["utc"] == sample_state.utc.isoformat()


def test_health_dict_carries_mode_and_source(client: TestClient) -> None:
    """Running: per-channel ``source`` badge + top-level ``mode``/``time_source``. Stopped: ``mode``
    from config, ``time_source`` omitted (no live clock to name)."""
    running = client.get("/healthz").json()
    assert running["mode"] == "simulate"
    assert "time_source" in running
    assert all("source" in c for c in running["channels"])

    assert client.post("/api/control", json={"action": "stop"}).status_code == 200
    stopped = client.get("/healthz").json()
    assert stopped["mode"] == "simulate"
    assert "time_source" not in stopped


# --- Phase C: the "state" SSE event ------------------------------------------------


def test_sse_stream_emits_state_event() -> None:
    """Drive ``GET /api/stream`` and assert a smooth ``state`` frame arrives (the ~4 Hz conning
    stream), carrying the conning keys. Same direct-ASGI harness as ``test_sse_stream_emits_events``
    (the sync TestClient buffers an infinite stream and deadlocks), bounded by ``wait_for``."""

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
                    if b"event: state" in captured["body"]:
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
                await asyncio.wait_for(got.wait(), timeout=8.0)
            finally:
                disconnect.set()
                await asyncio.wait_for(task, timeout=5.0)

            assert captured["status"] == 200
            body = captured["body"]
            assert b"event: state" in body
            # Pull the first state frame's JSON payload and assert the conning keys are present.
            marker = b"event: state\ndata: "
            start = body.index(marker) + len(marker)
            end = body.index(b"\n\n", start)
            payload = json.loads(body[start:end].decode())
            for key in ("app_wind_speed_kn", "app_wind_angle_deg", "pitch_deg", "roll_deg", "utc"):
                assert key in payload

    asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))


# --- Phase C: Save-as-defaults persist (POST /api/config/initial-state) ------------


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """A writable copy of the tracked baseline so persist writes never touch the repo file.

    ``create_app(str(tmp_config))`` makes both the load path and the persist target this tmp file
    (persist_path = config_path arg), so a Save-as-defaults write lands here deterministically —
    no reliance on the ``data/config.local.json`` cwd default."""
    dest = tmp_path / "config.json"
    shutil.copyfile(CONFIG_PATH, dest)
    return dest


def test_persist_initial_state_writes_and_reloads(tmp_config: Path) -> None:
    from nmea_sim.config import EngineConfig

    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post(
            "/api/config/initial-state",
            json={
                "stw_kn": 12.5,
                "depth_m": 50.0,
                "sea_state": 5.0,
                "mode": "simulate",
                "channels": [{"id": "ais", "enabled": False}],
                "inputs": [{"id": "gps_in", "function": "sat"}],
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["saved"] is True
        assert payload["hot_reloaded"] is False
        assert payload["mode"] == "simulate"
        assert payload["initial_state"]["stw_kn"] == pytest.approx(12.5)
        assert payload["initial_state"]["depth_m"] == pytest.approx(50.0)

    # The app wrote back to the tmp config (persist_path == config_path arg); reload reflects it.
    reloaded = EngineConfig.load(str(tmp_config))
    assert float(reloaded.initial_state_raw["stw_kn"]) == pytest.approx(12.5)
    assert float(reloaded.initial_state_raw["depth_m"]) == pytest.approx(50.0)
    assert float(reloaded.initial_state_raw["sea_state"]) == pytest.approx(5.0)
    assert reloaded.mode == "simulate"
    ais = next(ch for ch in reloaded.channels if ch.id == "ais")
    assert ais.enabled is False
    gps_in = next(i for i in reloaded.inputs if i.id == "gps_in")
    assert gps_in.function == "sat"


@pytest.mark.parametrize("bad_key", ["baud", "writer_backend", "path", "tcp_tap", "direction"])
def test_persist_rejects_disallowed_key(tmp_config: Path, bad_key: str) -> None:
    """``extra="forbid"`` turns any field outside the allow-list into a 422 — a request can never
    smuggle an I/O-redirecting or path-leaking field through this seam (R15)."""
    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post("/api/config/initial-state", json={"stw_kn": 5.0, bad_key: "x"})
        assert resp.status_code == 422


def test_persist_out_of_range_manual_field_is_bad_request(tmp_config: Path) -> None:
    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post("/api/config/initial-state", json={"sea_state": 12.0})
        assert resp.status_code == 400


def test_persist_unknown_channel_id_is_bad_request(tmp_config: Path) -> None:
    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post(
            "/api/config/initial-state", json={"channels": [{"id": "nope", "enabled": False}]}
        )
        assert resp.status_code == 400


def test_persist_unknown_input_id_is_bad_request(tmp_config: Path) -> None:
    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post(
            "/api/config/initial-state", json={"inputs": [{"id": "nope", "function": "gps"}]}
        )
        assert resp.status_code == 400


def test_persist_replay_mode_is_bad_request(tmp_config: Path) -> None:
    """``"replay"`` is a later feature the engine cannot honour — rejected 400, never persisted."""
    app = create_app(str(tmp_config))
    with TestClient(app) as c:
        resp = c.post("/api/config/initial-state", json={"mode": "replay"})
        assert resp.status_code == 400


# --- Phase C: GET /api/inputs (read-only, no device-path leak) ---------------------


def test_inputs_endpoint_shape_and_no_path_leak(client: TestClient) -> None:
    """Each slot exposes only id/function/detected_class/live/mismatch. In simulate mode (router
    None) every slot is idle. The raw device path MUST NOT appear anywhere in the response (R19)."""
    resp = client.get("/api/inputs")
    assert resp.status_code == 200
    slots = resp.json()
    assert {s["id"] for s in slots} == {"gps_in", "satcompass_in", "ais_in"}
    for s in slots:
        assert set(s) == {"id", "function", "detected_class", "live", "mismatch"}
        assert s["detected_class"] is None  # simulate mode: no router, no detection
        assert s["live"] is False
        assert s["mismatch"] is False

    # No slice of a configured device path may surface (path/by-id/ttyUSB/serial etc.).
    raw = resp.text
    assert "/dev/serial" not in raw
    assert "CHANGE-ME" not in raw


def test_inputs_endpoint_stopped_still_lists_slots(client: TestClient) -> None:
    assert client.post("/api/control", json={"action": "stop"}).status_code == 200
    resp = client.get("/api/inputs")
    assert resp.status_code == 200
    slots = resp.json()
    assert {s["id"] for s in slots} == {"gps_in", "satcompass_in", "ais_in"}
    assert all(s["detected_class"] is None and s["live"] is False for s in slots)


# --- Phase C: GET /api/security (read-only posture, NO secrets) --------------------


def test_security_endpoint_shape_no_secret_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The posture summary reports derived booleans only. Set BOTH app-Basic env vars (a real
    argon2 hash so the auth layer verifies) and assert ``app_basic`` is true while neither the hash
    value nor even the string ``HASH`` appears anywhere in the response body (R19)."""
    from web.app import _MAX_SUBSCRIBERS

    passlib_context = pytest.importorskip("passlib.context")
    pwd_context = passlib_context.CryptContext(schemes=["argon2"], deprecated="auto")
    try:
        password_hash = pwd_context.hash("s3cret-pass")
    except Exception as exc:  # pragma: no cover - environment without argon2 backend
        pytest.skip(f"argon2 backend unavailable: {exc!r}")

    monkeypatch.delenv("MOCKINGBUOY_BASIC_USER", raising=False)  # keep caddy_basic deterministic
    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_USER", "operator")
    monkeypatch.setenv("MOCKINGBUOY_APP_BASIC_HASH", password_hash)

    app = create_app(str(CONFIG_PATH))
    with TestClient(app) as c:
        resp = c.get("/api/security", auth=("operator", "s3cret-pass"))
        assert resp.status_code == 200
        body = resp.json()

        assert body["tls"] == "internal"
        assert body["app_basic"] is True
        assert body["caddy_basic"] is False
        assert body["app_bind"] == "127.0.0.1"
        assert body["tap_host"] == "127.0.0.1"
        assert body["max_subscribers"] == _MAX_SUBSCRIBERS
        assert body["running"] is True
        assert body["uptime_s"] >= 0.0
        assert isinstance(body["subscribers"], int)
        assert body["headers"] == []
        # config.json enables the instrument channel's tap on port 10110.
        assert {"channel": "instrument", "port": 10110} in body["taps"]

        raw = resp.text
        assert password_hash not in raw
        assert "s3cret-pass" not in raw
        assert "HASH" not in raw  # not even the env var NAME leaks
        assert "MOCKINGBUOY_APP_BASIC" not in raw
