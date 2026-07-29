from __future__ import annotations

from dataclasses import dataclass

from code_tree_exporter.extractors.package_support.oracle_parser import OraclePlsqlParser

@dataclass(frozen=True)
class TableReference:
    object_name: str
    operation: str
    edge_type: str
    start: int
    remote: bool
    db_link: str = False

@dataclass(frozen=True)
class CallReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class SequenceReference:
    object_name: str
    operation: str
    start: int

@dataclass(frozen=True)
class SqlAnalysis:
    tables: list[TableReference]
    calls: list[CallReference]
    sequences: list[SequenceReference]
    dynamic_offsets: list[int]
    parse_error_offsets: list[int]
    recognized: bool
    classification: str


def analyze_sql(text: str) -> SqlAnalysis:
    parser = OraclePlsqlParser(text)
    tables = []
    for reference in parser.table_references():
        remote = bool(reference.db_link)
        edge_type = "READS_FROM" if reference.relation == "READS" else "WRITES_TO" if reference.relation == "WRITES" else reference.relation
        tables.append(
            TableReference(
                reference.object_name,
                reference.operation,
                edge_type,
                reference.start,
                remote,
                reference.db_link,
            )
        )
    calls = [CallReference(reference.object_name, reference.start) for reference in parser.calls()]
    sequences = [
        SequenceReference(reference.object_name, reference.operation, reference.start)
        for reference in parser.sequences()
    ]
    dynamic_offsets = parser.dynamic_sql_offsets()
    parse_errors = _first_offset_per_line(
        text,
        {
            _offset_for_line_column(text, line, column)
            for line, column, _ in parser.syntax_errors
        },
    )
    return SqlAnalysis(
        tables,
        calls,
        sequences,
        dynamic_offsets,
        parse_errors,
        parser.has_executable_statement(),
        parser.script_classification(),
    )


def _offset_for_line_column(text: str, line: int, column: int) -> int:
    lines = text.splitlines(keepends=True)
    if line <= 0:
        return 0
    return min(sum(len(item) for item in lines[: line - 1]) + max(column, 0), len(text))

def _first_offset_per_line(text: str, offsets: set[int]) -> list[int]:
    first: dict[int, int] = {}
    for offset in sorted(offsets):
        line = text.count("\n", 0, offset)
        first.setdefault(line, offset)
    return list(first.values())

