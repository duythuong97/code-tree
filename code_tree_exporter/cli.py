from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractors import supported_source_types
from .pipeline import run_pipeline, validate_pipeline_config


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        prog="code-tree-exporter",
        description=(
            "Extract configured source units into graph packages, codebase memory, "
            "and RAG knowledge."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    extract_parser = commands.add_parser(
        "extract", description="Run all configured extractors and publish V3 output."
    )
    extract_parser.add_argument(
        "--config", help="Path to the unified JSON config."
    )
    extract_parser.add_argument(
        "--list-source-types", action="store_true", help=argparse.SUPPRESS
    )
    validate_parser = commands.add_parser(
        "validate", description="Validate V3 config and catalog without extracting."
    )
    validate_parser.add_argument("--config", required=True)

    catalog_parser = commands.add_parser(
        "catalog", description="Inspect and validate incoming catalog files."
    )
    catalog_commands = catalog_parser.add_subparsers(
        dest="catalog_command", required=True
    )
    inspect_parser = catalog_commands.add_parser(
        "inspect", description="Show headers and a profile template for one CSV."
    )
    inspect_parser.add_argument("path", help="CSV file to inspect.")
    inspect_parser.add_argument("--encoding", default="auto")

    # Keep the V2 invocation (`code-tree-exporter --config ...`) working.
    if arguments and arguments[0].startswith("-") and arguments[0] not in {
        "-h",
        "--help",
    }:
        arguments.insert(0, "extract")
    args = parser.parse_args(arguments)
    try:
        if args.command == "catalog":
            from .v3.catalog import inspect_catalog_file

            result = inspect_catalog_file(
                Path(args.path), encoding=str(args.encoding)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            result = validate_pipeline_config(Path(args.config))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.list_source_types:
            print("\n".join(supported_source_types()))
            return 0
        if not args.config:
            extract_parser.error("--config is required")
        output = run_pipeline(Path(args.config))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
