"""Capture unaltered local-browser frames for the TERX v0.5 demo video."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from terx.evals.local_suite import run_suite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory that will receive 01-task-ready.png through 03-tool-replayed.png.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    await run_suite(capture_dir=args.output)
    print(f"Captured reproducible local-demo frames in {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
