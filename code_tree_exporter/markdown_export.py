from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .graph_package import write_codebase_memory, write_knowledge_markdown
from .renderers import render_file_tree, render_system_tree
from .sqlite_graph import load_sqlite_graph, resolve_database_path

_MANAGED_PATHS = (
    "file-trees",
    "knowledge",
    "codebase-memory",
    "SYSTEM_TREE.md",
    "markdown-manifest.json",
)


def export_markdown(
    database: Path,
    output: Path | None = None,
    *,
    max_tree_lines: int | None = None,
    combined_projection: bool | None = None,
) -> Path:
    database = resolve_database_path(database)
    graph, metadata = load_sqlite_graph(database)
    options = metadata.get("markdown_options")
    options = options if isinstance(options, dict) else {}

    configured_max_lines = options.get("maxTreeLines", 20_000)
    if max_tree_lines is None:
        try:
            max_tree_lines = int(configured_max_lines)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid maxTreeLines stored in SQLite metadata") from exc
    if max_tree_lines < 10:
        raise ValueError("maxTreeLines must be at least 10")

    if combined_projection is None:
        configured_combined = options.get("combinedProjection")
        if isinstance(configured_combined, bool):
            combined_projection = configured_combined
        else:
            combined_projection = metadata.get("output_mode", "flat") == "flat"

    chunking = options.get("knowledgeChunking")
    chunking = chunking if isinstance(chunking, dict) else {}
    output = (output or database.parent).expanduser().resolve()
    if output == database:
        raise ValueError("Markdown output must be a directory, not the SQLite file")
    if output.exists() and not output.is_dir():
        raise ValueError(f"Markdown output is not a directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _validate_managed_targets(output)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(
        prefix=".markdown-export-", dir=output.parent
    ) as temporary:
        staging = Path(temporary)
        render_file_tree(graph, staging / "file-trees", max_tree_lines)
        if combined_projection:
            render_system_tree(
                graph, staging / "SYSTEM_TREE.md", max_tree_lines
            )
        knowledge_refs = write_knowledge_markdown(
            graph,
            staging,
            generated_at=generated_at,
            chunking=chunking,
        )
        memory_refs = write_codebase_memory(
            graph,
            staging,
            generated_at=generated_at,
            knowledge_refs=knowledge_refs,
        )
        if output == database.parent:
            _stage_updated_graph_index(
                database.parent / "graph-index.json",
                staging / "graph-index.json",
                knowledge_refs=knowledge_refs,
                memory_refs=memory_refs,
            )
        manifest = {
            "version": "1.0",
            "generatedAt": generated_at,
            "sourceDatabase": str(database),
            "options": {
                "combinedProjection": combined_projection,
                "maxTreeLines": max_tree_lines,
                "knowledgeChunking": chunking,
            },
            "statistics": {
                "sourceUnits": len(graph.source_descriptors),
                "files": len(graph.source_records),
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
            },
            "metadata": {"managedBy": "code-tree-exporter"},
        }
        (staging / "markdown-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for name in _MANAGED_PATHS:
            target = output / name
            staged = staging / name
            _remove_path(target)
            if staged.exists():
                staged.replace(target)
        staged_index = staging / "graph-index.json"
        if staged_index.exists():
            _remove_path(output / "graph-index.json")
            staged_index.replace(output / "graph-index.json")
    return output


def _validate_managed_targets(output: Path) -> None:
    if not any((output / name).exists() for name in _MANAGED_PATHS):
        return
    for manifest_path in (
        output / "markdown-manifest.json",
        output / "manifest.json",
    ):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        extractor = manifest.get("extractor") if isinstance(manifest, dict) else None
        metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
        if (
            isinstance(extractor, dict)
            and extractor.get("name") == "code-tree-exporter"
        ) or (
            isinstance(metadata, dict)
            and metadata.get("managedBy") == "code-tree-exporter"
        ):
            return
    raise ValueError(
        f"Refusing to replace unmanaged Markdown output in: {output}"
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _stage_updated_graph_index(
    source: Path,
    target: Path,
    *,
    knowledge_refs: dict[str, list[str]],
    memory_refs: dict[str, dict[str, object]],
) -> None:
    try:
        index = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(index, dict):
        return
    index["knowledgeByNodeId"] = knowledge_refs
    index["memoryByNodeId"] = memory_refs
    target.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Markdown projections from an extracted graph.sqlite."
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Path to graph.sqlite or its containing output directory.",
    )
    parser.add_argument(
        "--output",
        help="Markdown output directory. Defaults to the database directory.",
    )
    parser.add_argument("--max-tree-lines", type=int)
    projection = parser.add_mutually_exclusive_group()
    projection.add_argument(
        "--combined-projection",
        dest="combined_projection",
        action="store_true",
    )
    projection.add_argument(
        "--no-combined-projection",
        dest="combined_projection",
        action="store_false",
    )
    parser.set_defaults(combined_projection=None)
    args = parser.parse_args(argv)
    try:
        result = export_markdown(
            Path(args.database),
            Path(args.output) if args.output else None,
            max_tree_lines=args.max_tree_lines,
            combined_projection=args.combined_projection,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
