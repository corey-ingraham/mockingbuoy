"""CLI: distil an AIS export or capture into a realism profile JSON.

Usage::

    python -m nmea_sim.aisprofile <input> --out <path> \
        [--format csv|aivdm|auto] \
        [--min-lat --max-lat --min-lon --max-lon] \
        [--columns lat=Latitude lon=Longitude ...] \
        [--motion-model transiting|anchored|drifting]

``<input>`` is a file or a directory. With ``--format auto`` (the default) the source is sniffed
from its first non-empty line: a line beginning with ``!AIVDM``/``!AIVDO`` is treated as a
capture, anything else as CSV. The output is pretty-printed JSON that
:meth:`nmea_sim.realism.RealismProfile.from_dict` accepts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from . import aivdm_source, csv_source
from .profile import MOTION_MODELS, build_profile
from .records import AisRecord


def _first_nonempty_line(path: Path) -> str:
    """The first non-blank line of a file, or of the first readable file in a directory."""
    files = [path] if path.is_file() else sorted(p for p in path.iterdir() if p.is_file())
    for file in files:
        with file.open(encoding="latin-1", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line:
                    return line
    return ""


def _sniff_format(path: Path) -> str:
    """Pick ``aivdm`` when the first non-empty line is an AIVDM/AIVDO sentence, else ``csv``."""
    line = _first_nonempty_line(path)
    if line[:1] == "!" and line[1:6] in ("AIVDM", "AIVDO"):
        return "aivdm"
    return "csv"


def _parse_columns(pairs: list[str] | None) -> dict[str, str]:
    """Turn ``["lat=Latitude", "lon=Longitude"]`` into a logical-field -> column mapping."""
    mapping: dict[str, str] = {}
    for pair in pairs or []:
        field, sep, column = pair.partition("=")
        if not sep or not field or not column:
            raise SystemExit(f"--columns entries must be field=column, got {pair!r}")
        mapping[field] = column
    return mapping


def _bbox_from_args(args: argparse.Namespace) -> csv_source.BBox | None:
    parts = (args.min_lat, args.max_lat, args.min_lon, args.max_lon)
    if all(p is None for p in parts):
        return None
    if any(p is None for p in parts):
        raise SystemExit("bbox needs all four of --min-lat/--max-lat/--min-lon/--max-lon or none")
    return (args.min_lat, args.max_lat, args.min_lon, args.max_lon)


def _records(args: argparse.Namespace, fmt: str) -> Iterator[AisRecord]:
    if fmt == "aivdm":
        return aivdm_source.iter_records(args.input)
    return csv_source.iter_records(
        args.input,
        columns=_parse_columns(args.columns),
        bbox=_bbox_from_args(args),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nmea_sim.aisprofile",
        description="Distil an AIS CSV export or NMEA capture into a realism profile.",
    )
    parser.add_argument("input", type=Path, help="A CSV/capture file or a directory of them.")
    parser.add_argument("--out", type=Path, required=True, help="Output profile JSON path.")
    parser.add_argument(
        "--format",
        choices=("csv", "aivdm", "auto"),
        default="auto",
        help="Source format; 'auto' sniffs the first non-empty line (default).",
    )
    parser.add_argument("--min-lat", type=float, default=None)
    parser.add_argument("--max-lat", type=float, default=None)
    parser.add_argument("--min-lon", type=float, default=None)
    parser.add_argument("--max-lon", type=float, default=None)
    parser.add_argument(
        "--columns",
        nargs="*",
        metavar="FIELD=COLUMN",
        help="Override CSV column names, e.g. --columns lat=Latitude lon=Longitude.",
    )
    parser.add_argument("--motion-model", choices=MOTION_MODELS, default="transiting")
    args = parser.parse_args(argv)

    if not args.input.exists():
        sys.stderr.write(f"aisprofile: input not found: {args.input}\n")
        return 2

    fmt = _sniff_format(args.input) if args.format == "auto" else args.format

    # The CSV-only filters have no meaning for a decoded AIVDM capture; fail loud rather than
    # silently ignoring them (a bbox would also drop static-only records and lose ship types).
    if fmt == "aivdm":
        if any(p is not None for p in (args.min_lat, args.max_lat, args.min_lon, args.max_lon)):
            sys.stderr.write(
                "aisprofile: --min/max-lat/lon is a CSV-only filter, "
                "unsupported for aivdm captures\n"
            )
            return 2
        if args.columns:
            sys.stderr.write(
                "aisprofile: --columns applies only to CSV inputs, not aivdm captures\n"
            )
            return 2

    try:
        profile = build_profile(_records(args, fmt), motion_model=args.motion_model)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"aisprofile: {exc}\n")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(f"wrote profile ({fmt}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
