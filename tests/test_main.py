"""Headless main.py CLI: validate-only, invalid-config exit codes, a bounded null run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import main as cli

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def test_validate_only_on_example_config_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--config", str(CONFIG_PATH), "--validate-only"])
    assert rc == 0
    assert "is valid" in capsys.readouterr().err


def test_missing_config_exits_two() -> None:
    try:
        cli.main(["--config", "does-not-exist.json", "--validate-only"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - must raise
        raise AssertionError("expected SystemExit(2)")


def test_invalid_config_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "initial_state": {"lat": 0.0, "lon": 0.0},
                "channels": [
                    {
                        "id": "x",
                        "role": "gps",
                        "path": "/dev/serial/by-id/x",
                        "baud": 4800,
                        "talker": "GP",
                        "emit": [{"sentence": "HDT", "rate_hz": 1.0}],  # illegal for gps
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["--config", str(bad), "--validate-only"])
    assert rc == 1
    assert "cannot emit 'HDT'" in capsys.readouterr().err


def test_bounded_null_run_completes(capsys: pytest.CaptureFixture[str]) -> None:
    # backend=null emits nowhere; --duration bounds the run so the test is deterministic.
    rc = cli.main(["--config", str(CONFIG_PATH), "--backend", "null", "--duration", "0.3"])
    assert rc == 0
    assert "running 3 channel(s)" in capsys.readouterr().err
