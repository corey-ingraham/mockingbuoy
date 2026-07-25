"""Packaging / dependency-floor regression guards.

These lock in the fixes for the ops/packaging findings so they cannot silently regress:

* pyais floor is >= 3.1 (the code needs the 3.x ``sentence_type`` API) in both the
  installable metadata (pyproject) and the compile input (requirements.in).
* The wheel/sdist ships the web UI via package-data (a bare ``pip install`` must serve
  ``GET /`` — without the glob the build omits ``web/static/*`` and the root route 500s).
* requirements.in pins ``uvloop`` with a non-Windows marker so a recompile always records
  it (uvicorn's ``standard`` extra needs it on Linux; a Windows compile silently drops it).
* config.json's schema note points operators at ``data/config.local.json`` — the only local
  override the app actually loads.

Pure file inspection: deterministic, no network, no wall-clock.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict[str, Any]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _min_version(spec: str) -> tuple[int, ...]:
    """Extract the ``>=`` floor from a requirement spec like ``pyais>=3.1`` -> (3, 1)."""
    match = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", spec)
    assert match is not None, f"no >= floor found in spec: {spec!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_pyais_floor_in_pyproject() -> None:
    deps = _load_pyproject()["project"]["dependencies"]
    pyais = next((d for d in deps if d.replace(" ", "").startswith("pyais")), None)
    assert pyais is not None, "pyais missing from pyproject dependencies"
    assert _min_version(pyais) >= (3, 1), f"pyais floor must be >= 3.1, got {pyais!r}"


def test_pyais_floor_in_requirements_in() -> None:
    text = (_REPO_ROOT / "requirements.in").read_text(encoding="utf-8")
    line = next(
        (ln for ln in text.splitlines() if ln.strip().replace(" ", "").startswith("pyais")),
        None,
    )
    assert line is not None, "pyais missing from requirements.in"
    assert _min_version(line) >= (3, 1), f"pyais floor must be >= 3.1, got {line!r}"


def test_web_static_shipped_as_package_data() -> None:
    pkg_data = _load_pyproject()["tool"]["setuptools"]["package-data"]
    web_globs = pkg_data.get("web", [])
    # A glob (static/*), not just index.html, so renamed/added assets ship too.
    assert any(
        g.startswith("static/") for g in web_globs
    ), f"web package-data must ship static/* , got {web_globs!r}"


def test_uvloop_pinned_with_non_windows_marker() -> None:
    text = (_REPO_ROOT / "requirements.in").read_text(encoding="utf-8")
    line = next(
        (ln for ln in text.splitlines() if ln.strip().startswith("uvloop")),
        None,
    )
    assert line is not None, "uvloop must be pinned in requirements.in (uvicorn[standard])"
    normalized = line.replace(" ", "")
    assert 'sys_platform!="win32"' in normalized, f"uvloop needs a non-win32 marker: {line!r}"


def test_config_note_points_at_data_local() -> None:
    with (_REPO_ROOT / "config.json").open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    schema_note = cfg.get("$schema_note", "")
    assert (
        "data/config.local.json" in schema_note
    ), "config.json schema note must standardize on data/config.local.json"
