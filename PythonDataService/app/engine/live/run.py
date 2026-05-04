"""Command-line entrypoint for the live runtime."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Build the live-runtime CLI parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.engine.live.run",
        description="Run the SPY EMA crossover strategy against IBKR paper trading.",
    )
    parser.add_argument("--config", help="Optional paper runtime config file.", default=None)
    return parser


def main() -> None:
    """Parse CLI args.

    Phase 8 wires execution; Phase 1 only proves the entrypoint imports.
    """
    build_parser().parse_args()


if __name__ == "__main__":
    main()

