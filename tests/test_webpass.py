"""Phase C: in-app web-password rotation — app-side protocol tests.

These exercise the app side of the rotation protocol WITHOUT a real caddy binary or the root
systemd path-unit. ``_caddy_hash`` is stubbed to return a canned bcrypt-shaped hash, and the
root unit's result file is simulated by writing ``data/webpass-result.json`` directly.

§9 invariant under test: only a bcrypt HASH ever lands on disk. The throwaway plaintext used
here is a random, in-memory, non-secret token (``secrets.token_hex``); every test asserts it
never appears in any persisted file, in argv, in a response body, or in an error message.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import web.app as web_app
from web.app import create_app

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# A canned bcrypt-shaped hash: ``$2b$`` + 2-digit cost + ``$`` + 53 chars. Not a secret; a fixed
# stand-in for whatever ``caddy hash-password`` would emit. Matches the app-side format check.
_CANNED_HASH = "$2b$12$" + "a" * 53


@pytest.fixture
def webpass_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    """A TestClient plus an isolated tmp ``data/`` dir, with the caddy hash stubbed so no
    external process (caddy) and no root path-unit are needed. The handlers read
    ``_DIAG_DATA_DIR`` and ``_caddy_hash`` as module globals at call time, so patching after
    app construction takes effect for every request."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "_DIAG_DATA_DIR", str(data_dir))
    monkeypatch.setattr(web_app, "_caddy_hash", lambda _pw: _CANNED_HASH)
    with TestClient(create_app(str(CONFIG_PATH))) as c:
        yield c, data_dir


def test_rotate_password_rejects_short_password(webpass_ctx: tuple[TestClient, Path]) -> None:
    """A new password under the 12-char policy is refused 400. The detail names the POLICY,
    never the value; no request file is written; nothing plaintext is persisted."""
    c, data_dir = webpass_ctx
    short = "a" * 11  # 11 chars, one under the floor; a filler literal, not a secret
    resp = c.post("/api/security/rotate-password", json={"new_password": short})
    assert resp.status_code == 400
    assert "12 characters" in resp.json()["detail"]
    assert short not in resp.text  # the value never echoes back
    assert not (data_dir / web_app._WEBPASS_REQUEST).exists()


def test_rotate_password_writes_hash_only_request(
    webpass_ctx: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid rotation drops ``data/webpass-request.json`` holding ONLY a bcrypt hash + nonce +
    ts — never the plaintext — at mode 0600 (POSIX). We drive the poll to time out fast so the
    request file is left in place for inspection (the root unit would normally consume it)."""
    c, data_dir = webpass_ctx
    monkeypatch.setattr(web_app, "_WEBPASS_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(web_app, "_WEBPASS_POLL_INTERVAL_S", 0.01)
    throwaway = secrets.token_hex(8)  # 16-char random in-memory value, never a real secret

    resp = c.post("/api/security/rotate-password", json={"new_password": throwaway})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}  # no result file => pending

    req_path = data_dir / web_app._WEBPASS_REQUEST
    assert req_path.exists()
    raw = req_path.read_text(encoding="utf-8")
    record = json.loads(raw)
    assert record["hash"] == _CANNED_HASH
    assert record["hash"].startswith("$2")
    assert isinstance(record["nonce"], str) and len(record["nonce"]) == 32
    assert isinstance(record["ts"], (int, float))
    # §9: the plaintext must NOT appear anywhere in the persisted request.
    assert throwaway not in raw
    assert record.get("new_password") is None and record.get("password") is None
    if os.name == "posix":  # Windows cannot represent 0600; the real perm is verified on the Pi
        assert stat.S_IMODE(os.stat(req_path).st_mode) == 0o600


def test_rotate_password_pending_when_no_result(
    webpass_ctx: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no root unit result before the bound, the app returns ``pending`` — but the marker was
    written OPTIMISTICALLY (a dropped connection is the normal success path and is indistinguishable
    from pending here), so it is present. Only an observed ``failure`` retracts it."""
    c, data_dir = webpass_ctx
    monkeypatch.setattr(web_app, "_WEBPASS_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(web_app, "_WEBPASS_POLL_INTERVAL_S", 0.01)

    resp = c.post("/api/security/rotate-password", json={"new_password": secrets.token_hex(8)})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}
    assert (data_dir / web_app._WEBPASS_MARKER).exists()


def _simulate_root_result(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, status: str, detail: str | None = None
) -> None:
    """Patch ``_write_webpass_request`` so that the moment the app drops its request, a matching-
    nonce result file appears — standing in for the root path-unit's synchronous reply."""
    original = web_app._write_webpass_request

    def _fake(record: dict[str, Any]) -> None:
        original(record)  # still writes the real 0600 request (hash-only) for realism
        result: dict[str, Any] = {"nonce": record["nonce"], "status": status}
        if detail is not None:
            result["detail"] = detail
        (data_dir / web_app._WEBPASS_RESULT).write_text(json.dumps(result), encoding="utf-8")

    monkeypatch.setattr(web_app, "_write_webpass_request", _fake)


def test_rotate_password_ok_writes_marker(
    webpass_ctx: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the (simulated) root unit reports ``ok`` for the matching nonce, the app returns ok
    and writes the ``.webpass_changed`` marker. No plaintext lands in any file in ``data/``."""
    c, data_dir = webpass_ctx
    _simulate_root_result(monkeypatch, data_dir, "ok")
    throwaway = secrets.token_hex(8)

    resp = c.post("/api/security/rotate-password", json={"new_password": throwaway})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (data_dir / web_app._WEBPASS_MARKER).exists()

    # Scrub: no file in the writable dir may contain the throwaway plaintext.
    for f in data_dir.iterdir():
        if f.is_file():
            assert throwaway not in f.read_text(encoding="utf-8", errors="ignore")


def test_rotate_password_failure_no_marker(
    webpass_ctx: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``failure`` result surfaces the fixed detail and does NOT write the marker (the banner
    persists and the prior password still works)."""
    c, data_dir = webpass_ctx
    _simulate_root_result(monkeypatch, data_dir, "failure", detail="health")

    resp = c.post("/api/security/rotate-password", json={"new_password": secrets.token_hex(8)})
    assert resp.status_code == 200
    assert resp.json() == {"status": "failure", "detail": "health"}
    assert not (data_dir / web_app._WEBPASS_MARKER).exists()


def test_security_password_is_default_flips_with_marker(
    webpass_ctx: tuple[TestClient, Path],
) -> None:
    """``GET /api/security`` reports ``password_is_default`` true while the marker is absent and
    false once it exists (a bare existence probe — no secret)."""
    c, data_dir = webpass_ctx
    assert c.get("/api/security").json()["password_is_default"] is True

    (data_dir / web_app._WEBPASS_MARKER).write_bytes(b"")
    assert c.get("/api/security").json()["password_is_default"] is False


def test_dismiss_default_prompt_writes_marker(webpass_ctx: tuple[TestClient, Path]) -> None:
    """The banner's "I've already changed it" button writes the marker and flips the posture,
    with no request body and no secret involved."""
    c, data_dir = webpass_ctx
    resp = c.post("/api/security/dismiss-default-prompt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (data_dir / web_app._WEBPASS_MARKER).exists()
    assert c.get("/api/security").json()["password_is_default"] is False


def test_caddy_hash_feeds_plaintext_on_stdin_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_caddy_hash`` must feed the plaintext to caddy on STDIN and NEVER place it in argv (§9).
    ``subprocess.run`` is stubbed to capture the call and return a canned bcrypt hash — no real
    caddy process is spawned."""
    captured: dict[str, Any] = {}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = list(args[0])
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=(_CANNED_HASH + "\n").encode(), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    throwaway = secrets.token_hex(8)

    out = web_app._caddy_hash(throwaway)
    assert out == _CANNED_HASH
    # argv carries only the command + flags — never the plaintext.
    assert "hash-password" in captured["argv"]
    assert "bcrypt" in captured["argv"]
    assert all(throwaway not in part for part in captured["argv"])
    # The plaintext reaches caddy only via stdin (newline-terminated).
    assert captured["input"] == (throwaway + "\n").encode()


def test_caddy_hash_raises_generic_on_bad_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-bcrypt stdout is rejected with a generic error (no plaintext, no caddy stderr)."""

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=b"not-a-hash\n", stderr=b"noise"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    throwaway = secrets.token_hex(8)
    with pytest.raises(RuntimeError) as exc:
        web_app._caddy_hash(throwaway)
    assert throwaway not in str(exc.value)
    assert "noise" not in str(exc.value)
