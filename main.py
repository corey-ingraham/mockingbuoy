"""mockingbuoy entry point — headless engine runner and config validator.

This is the no-web, no-hardware way to drive the engine: load a config, validate it, and
run the synchronized GPS/heading/AIS generators against a chosen backend. It exists so the
engine can be exercised (and its config validated) from a shell or a systemd unit without
FastAPI, a browser, or a serial adapter.

Usage::

    python main.py --validate-only                 # parse + deep-validate, print problems, exit
    python main.py --backend log                   # print every sentence to stdout (default)
    python main.py --backend null --duration 5     # run 5s emitting nowhere (timing smoke test)
    python main.py --config config.local.json      # use a specific config file

The web front end (P6) imports the same engine; this module never imports it, so the engine
core stays free of any web/uvicorn dependency.
"""

from __future__ import annotations

import argparse
import dataclasses
import signal
import sys
import threading

from nmea_sim.config import EngineConfig
from nmea_sim.engine import BudgetExceeded, Engine
from nmea_sim.validate import ConfigError

_BACKENDS = ("log", "null", "pty", "serial")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mockingbuoy",
        description="Headless NMEA 0183 simulator/generator runner and config validator.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the JSON config (default: config.json).",
    )
    parser.add_argument(
        "--backend",
        choices=_BACKENDS,
        default=None,
        help="Override the config's writer_backend (log|null|pty|serial).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Run for this many seconds then stop; omit to run until interrupted (Ctrl-C).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Load and deep-validate the config, print any problems, and exit.",
    )
    parser.add_argument(
        "--no-strict-budget",
        action="store_true",
        help="Warn (do not abort) when a channel exceeds its baud budget.",
    )
    return parser.parse_args(argv)


def _load(path: str) -> EngineConfig:
    """Load a config, exiting non-zero with a readable message on any parse failure."""
    try:
        return EngineConfig.load(path)
    except FileNotFoundError:
        sys.stderr.write(f"mockingbuoy: config not found: {path}\n")
        raise SystemExit(2) from None
    except (ValueError, KeyError) as exc:
        sys.stderr.write(f"mockingbuoy: could not parse {path}: {exc}\n")
        raise SystemExit(2) from None


def _report_problems(problems: list[str]) -> None:
    sys.stderr.write(f"mockingbuoy: config invalid ({len(problems)} problem(s)):\n")
    for problem in problems:
        sys.stderr.write(f"  - {problem}\n")


def _run(engine: Engine, duration: float | None) -> None:
    """Start the engine and block until ``duration`` elapses or SIGINT/SIGTERM arrives."""
    stop = threading.Event()

    def _signal(_signum: int, _frame: object) -> None:
        stop.set()

    # SIGINT is always present; SIGTERM (systemd's stop signal) is registered when available.
    signal.signal(signal.SIGINT, _signal)
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signal.signal(sigterm, _signal)

    engine.start()
    try:
        stop.wait(duration)  # None => wait forever (until a signal fires)
    finally:
        engine.stop()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    config = _load(args.config)
    if args.backend is not None:
        config = dataclasses.replace(config, writer_backend=args.backend)

    problems = config.validate()
    if problems:
        _report_problems(problems)
        return 1
    if args.validate_only:
        sys.stderr.write(f"mockingbuoy: {args.config} is valid.\n")
        return 0

    try:
        engine = Engine(config, strict_budget=not args.no_strict_budget)
    except BudgetExceeded as exc:
        sys.stderr.write(f"mockingbuoy: {exc}\n")
        return 1

    sys.stderr.write(
        f"mockingbuoy: running {len(config.channels)} channel(s) on backend "
        f"{config.writer_backend!r}"
        + (f" for {args.duration:g}s" if args.duration is not None else " (Ctrl-C to stop)")
        + "\n"
    )
    try:
        _run(engine, args.duration)
    except ConfigError as exc:  # defensive: validation already ran, but never crash raw
        sys.stderr.write(f"mockingbuoy: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
