from __future__ import annotations

import argparse
import csv
from pathlib import Path

NAME_FIELDS = ("technical_name", "qualified_name", "default_display_name")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def find_nodes(nodes: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    needle = query.casefold()
    exact = [
        node
        for node in nodes
        if any(node[field].casefold() == needle for field in NAME_FIELDS)
    ]
    return exact or [
        node
        for node in nodes
        if any(needle in node[field].casefold() for field in NAME_FIELDS)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tìm cách một node được sử dụng theo tên."
    )
    parser.add_argument(
        "name", help="Tên kỹ thuật, tên đầy đủ, hoặc tên hiển thị của node"
    )
    parser.add_argument(
        "--package-dir",
        type=Path,
        default=Path("dist/code-map-demo"),
        help="Thư mục chứa manifest.json và các CSV graph",
    )
    args = parser.parse_args()

    nodes = read_group(args.package_dir, "nodes")
    edges = read_group(args.package_dir, "edges")
    matches = find_nodes(nodes, args.name)
    if not matches:
        print(f"Không tìm thấy node có tên: {args.name}")
        return 1

    names_by_id = {node["node_id"]: node["default_display_name"] for node in nodes}
    for node in matches:
        node_id = node["node_id"]
        usage = [
            edge
            for edge in edges
            if node_id in (edge["source_node_id"], edge["target_node_id"])
        ]
        print(
            f"\n{node['default_display_name']} [{node['node_type']}]\n  id: {node_id}"
        )
        if not usage:
            print("  Không có quan hệ sử dụng.")
            continue

        for edge in usage:
            outgoing = edge["source_node_id"] == node_id
            other_id = edge["target_node_id"] if outgoing else edge["source_node_id"]
            direction = "dùng" if outgoing else "được dùng bởi"
            operation = f"/{edge['raw_operation']}" if edge["raw_operation"] else ""
            other_name = names_by_id.get(other_id, other_id)
            print(
                f"  {direction}: {other_name} -- {edge['edge_type']}{operation} ({edge['confidence']})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
