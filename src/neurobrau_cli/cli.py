"""Command-line entry point for NeuroBräu CLI."""

import argparse
from collections.abc import Sequence

from neurobrau_cli import __version__

PROG_NAME = "neurobrau"


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Educational multi-provider LLM command-line client.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
