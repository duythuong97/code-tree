from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .extractors import supported_source_types
from .pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract configured source units into graph packages, codebase memory, "
            "and RAG knowledge."
        )
    )
    parser.add_argument(
        "--config", required=True, help="Path to the unified JSON config."
    )
    parser.add_argument(
        "--list-source-types", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.list_source_types:
        print("\n".join(supported_source_types()))
        return 0
    try:
        output = run_pipeline(Path(args.config))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
