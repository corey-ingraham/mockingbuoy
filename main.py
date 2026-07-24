"""mockingbuoy entry point.

Dispatches between the headless engine runner and the web app. This is the scaffold entry so the
package layout and the ``mockingbuoy`` console script resolve; the full CLI (backends, headless vs
web, config loading) is implemented in the config/headless and web phases.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Entry point. Full CLI lands in later phases (see the build plan, P5/P6)."""
    args = sys.argv[1:] if argv is None else argv
    sys.stderr.write(
        "mockingbuoy: CLI not yet implemented (scaffold). "
        f"args={args!r}. See the build plan (P5/P6).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
