#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql


def main() -> int:
    for line in sys.stdin:
        request = json.loads(line)
        analysis = analyze_sql(request["text"])
        print(
            json.dumps(
                {
                    "Tables": [
                        {
                            "ObjectName": item.object_name,
                            "Operation": item.operation,
                            "EdgeType": item.edge_type,
                            "Start": item.start,
                            "Remote": item.remote,
                            "DbLink": item.db_link,
                        }
                        for item in analysis.tables
                    ],
                    "Procedures": [
                        {"ObjectName": item.object_name, "Start": item.start}
                        for item in analysis.calls
                    ],
                    "Sequences": [
                        {
                            "ObjectName": item.object_name,
                            "Operation": item.operation,
                            "Start": item.start,
                        }
                        for item in analysis.sequences
                    ],
                    "DynamicOffsets": analysis.dynamic_offsets,
                    "ParseErrorOffsets": analysis.parse_error_offsets,
                    "Recognized": analysis.recognized,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
