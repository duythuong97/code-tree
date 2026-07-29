from __future__ import annotations

import json
import re
from dataclasses import dataclass

from code_tree_exporter.contract.graph_contract import column_id, table_id
from code_tree_exporter.extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    leaf_identifier,
    line_text,
    unresolved_id,
)
from code_tree_exporter.extractors.package_support.semantic_tree_v3 import analysis_notes

_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$#]*)(?:\s*\.\s*(?:"(?:[^"]|"")+"|[A-Za-z_][\w$#]*))?'
_FILE_CLAUSE_RE = re.compile(r"\b(INFILE|BADFILE|DISCARDFILE)\s+(?:'((?:''|[^'])*)'|\"((?:\"\"|[^\"])*)\"|([^\s]+))", re.IGNORECASE)
_INTO_RE = re.compile(rf"\bINTO\s+TABLE\s+({_IDENTIFIER})", re.IGNORECASE)
_MODE_RE = re.compile(r"\b(INSERT|APPEND|REPLACE|TRUNCATE)\b", re.IGNORECASE)
_FIELD_RE = re.compile(r'^\s*("(?:[^"]|"")+"|[A-Za-z_][\w$#]*)(.*)$', re.DOTALL)


@dataclass(frozen=True)
class LoaderFileClause:
    kind: str
    value: str
    line: int
    raw: str

@dataclass(frozen=True)
class LoaderField:
    column: str
    line: int
    source_field: str
    position: str = ""
    datatype: str = ""
    transform: str = ""
    constant: str = ""
    filler: bool = False
    filler_kind: str = ""
    nullif: str = ""
    defaultif: str = ""
    sequence: str = ""
    generated: str = ""
    raw_spec: str = ""


@dataclass(frozen=True)
class LoaderTarget:
    table: str
    mode: str
    line: int
    fields: tuple[LoaderField, ...]
    when: str = ""
    trailing_nullcols: bool = False
    raw_clause: str = ""


@dataclass(frozen=True)
class LoaderControl:
    infile: str
    badfile: str
    discardfile: str
    targets: tuple[LoaderTarget, ...]
    errors: tuple[tuple[int, str], ...]
    files: tuple[LoaderFileClause, ...] = ()
    options_raw: str = ""
    options_line: int = 1
    raw_header: str = ""


def parse_sql_loader(text: str) -> LoaderControl:
    """Parse the common SQL*Loader control-file subset without hiding unsupported syntax."""
    scan = _before_begindata(text)
    errors: list[tuple[int, str]] = []
    if not re.search(r"\bLOAD\s+DATA\b", scan, re.IGNORECASE):
        errors.append((1, "SQL*Loader control file is missing LOAD DATA"))

    files = {"INFILE": "", "BADFILE": "", "DISCARDFILE": ""}
    file_clauses = []
    for match in _FILE_CLAUSE_RE.finditer(scan):
        kind = match.group(1).upper()
        value = (match.group(2) or match.group(3) or match.group(4) or "").replace("''", "'").replace('""', '"')
        files[kind] = value
        file_clauses.append(LoaderFileClause(kind, value, _line(scan, match.start()), match.group(0)))
    options_raw, options_line = _parenthesized_clause(scan, "OPTIONS")

    into_matches = list(_INTO_RE.finditer(scan))
    if not into_matches:
        errors.append((1, "SQL*Loader control file is missing INTO TABLE"))
    prefix = scan[:into_matches[0].start()] if into_matches else scan
    global_mode_match = _MODE_RE.search(prefix)
    global_mode = global_mode_match.group(1).upper() if global_mode_match else "INSERT"

    targets: list[LoaderTarget] = []
    for index, match in enumerate(into_matches):
        end = into_matches[index + 1].start() if index + 1 < len(into_matches) else len(scan)
        segment = scan[match.end():end]
        open_at = _field_list_open(segment)
        header = segment[:open_at]
        mode_match = _MODE_RE.search(header)
        mode = mode_match.group(1).upper() if mode_match else global_mode
        when_match = re.search(r"\bWHEN\b(.+?)(?=\b(?:INSERT|APPEND|REPLACE|TRUNCATE|TRAILING\s+NULLCOLS)\b|$)", header, re.IGNORECASE | re.DOTALL)
        fields: tuple[LoaderField, ...] = ()
        if open_at < len(segment):
            close_at = _matching_paren(segment, open_at)
            if close_at is None:
                errors.append((_line(scan, match.end() + open_at), f"Unclosed field list for {match.group(1)}"))
            else:
                block_start = match.end() + open_at + 1
                fields = tuple(_parse_fields(segment[open_at + 1:close_at], scan, block_start))
        targets.append(LoaderTarget(
            table=_clean_identifier(match.group(1)),
            mode=mode,
            line=_line(scan, match.start()),
            fields=fields,
            when=" ".join(when_match.group(1).split()) if when_match else "",
            trailing_nullcols=bool(re.search(r"\bTRAILING\s+NULLCOLS\b", segment, re.IGNORECASE)),
            raw_clause=" ".join((match.group(0) + segment).split()),
        ))

    return LoaderControl(
        files["INFILE"], files["BADFILE"], files["DISCARDFILE"], tuple(targets), tuple(errors),
        tuple(file_clauses), options_raw, options_line, " ".join(prefix.split()),
    )


def _parse_fields(block: str, full_text: str, block_start: int) -> list[LoaderField]:
    fields: list[LoaderField] = []
    for raw, offset in _split_top_level(block):
        match = _FIELD_RE.match(raw)
        if not match:
            continue
        column = _clean_identifier(match.group(1))
        tail = " ".join(match.group(2).split())
        upper = tail.upper()
        filler_match = re.search(r"\b(BOUNDFILLER|FILLER)\b", upper)
        position_match = re.search(r"\bPOSITION\s*\(([^)]+)\)", tail, re.IGNORECASE)
        constant_match = re.search(r"\bCONSTANT\s+(?:'((?:''|[^'])*)'|([^\s,]+))", tail, re.IGNORECASE)
        sequence_match = re.search(r"\bSEQUENCE\s*\(([^)]*)\)", tail, re.IGNORECASE)
        generated_match = re.search(r"\b(RECNUM|SYSDATE)\b", tail, re.IGNORECASE)
        nullif = _field_clause(tail, "NULLIF", ("DEFAULTIF", "CONSTANT"))
        defaultif = _field_clause(tail, "DEFAULTIF", ("NULLIF", "CONSTANT"))
        quoted = re.findall(r'"((?:""|[^"])*)"', tail)
        transform = quoted[-1].replace('""', '"') if quoted and not constant_match else ""
        datatype_match = re.search(r"\b(INTEGER\s+EXTERNAL|DECIMAL\s+EXTERNAL|ZONED\s+DECIMAL|PACKED\s+DECIMAL|CHAR|DATE|TIMESTAMP|FLOAT|DOUBLE|RAW|VARRAW|VARCHAR|VARCHARC)\b", tail, re.IGNORECASE)
        constant = (constant_match.group(1) or constant_match.group(2) or "").replace("''", "'") if constant_match else ""
        generated = generated_match.group(1).upper() if generated_match else ""
        sequence = sequence_match.group(1).strip() if sequence_match else ""
        fields.append(LoaderField(
            column=column,
            line=_line(full_text, block_start + offset + len(raw) - len(raw.lstrip())),
            source_field="" if constant or sequence or generated else column,
            position=position_match.group(1).strip() if position_match else "",
            datatype=" ".join(datatype_match.group(1).upper().split()) if datatype_match else "",
            transform=transform,
            constant=constant,
            filler=bool(filler_match),
            filler_kind=filler_match.group(1).upper() if filler_match else "",
            nullif=nullif,
            defaultif=defaultif,
            sequence=sequence,
            generated=generated,
            raw_spec=" ".join(raw.split()),
        ))
    return fields


def _split_top_level(text: str) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    start = 0
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            result.append((text[start:index], start))
            start = index + 1
        index += 1
    if text[start:].strip():
        result.append((text[start:], start))
    return result


def _field_list_open(text: str) -> int:
    """Return the last top-level group; earlier groups belong to clauses such as WHEN."""
    opens = []
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            if depth == 0:
                opens.append(index)
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        index += 1
    return opens[-1] if opens else len(text)

def _parenthesized_clause(text: str, keyword: str) -> tuple[str, int]:
    match = re.search(rf"\b{keyword}\s*\(", text, re.IGNORECASE)
    if not match:
        return "", 1
    open_at = text.find("(", match.start())
    close_at = _matching_paren(text, open_at)
    raw = text[match.start():close_at + 1] if close_at is not None else text[match.start():]
    return " ".join(raw.split()), _line(text, match.start())

def _field_clause(text: str, keyword: str, stops: tuple[str, ...]) -> str:
    stop_pattern = "|".join(re.escape(stop) for stop in stops)
    match = re.search(rf"\b{keyword}\b\s+(.+?)(?=\s+\b(?:{stop_pattern})\b|$)", text, re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else ""


def _matching_paren(text: str, start: int) -> int | None:
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _before_begindata(text: str) -> str:
    match = re.search(r"(?im)^\s*BEGINDATA\b", text)
    return text[:match.start()] if match else text


def _clean_identifier(value: str) -> str:
    return ".".join(part.strip().strip('"').replace('""', '"') for part in re.split(r"\s*\.\s*", value))


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _loader_config_fact(control: LoaderControl, source_path: str) -> dict:
    steps = [
        {
            "type": "loader_file",
            "label": clause.kind,
            "source": {"path": source_path, "line": clause.line},
            "action": clause.kind,
            "expression": clause.value,
            "raw_clause": clause.raw,
        }
        for clause in control.files
    ]
    if control.options_raw:
        steps.insert(0, {
            "type": "loader_options",
            "label": "OPTIONS",
            "source": {"path": source_path, "line": control.options_line},
            "expression": control.options_raw,
        })
    return {
        "type": "loader_config",
        "label": "SQL*Loader configuration",
        "source": {"path": source_path, "line": 1},
        "expression": control.raw_header,
        "resolution": "partial",
        "steps": steps,
    }

def _field_fact(field: LoaderField, source_path: str, database: str, table_name: str, catalog: Catalog) -> dict:
    has_column = not field.filler and catalog.has_table(database, table_name) and catalog.has_column(database, table_name, field.column)
    fact = {
        "type": "field_mapping",
        "label": field.column,
        "source": {"path": source_path, "line": field.line},
        "target": field.column,
        "resolution": "resolved" if has_column else "partial" if field.filler or field.sequence or field.generated else "unresolved",
        "raw_spec": field.raw_spec,
        **{key: value for key, value in {
            "source_field": field.source_field,
            "position": field.position,
            "datatype": field.datatype,
            "transform": field.transform,
            "constant": field.constant,
            "filler_kind": field.filler_kind,
            "nullif": field.nullif,
            "defaultif": field.defaultif,
            "sequence": field.sequence,
            "generated": field.generated,
        }.items() if value},
    }
    if has_column:
        fact["ref_node_id"] = column_id(database, table_name, field.column)
    return fact

def extract_sql_loader(builder: PackageBuilder, catalog: Catalog, sql_id: str, text: str, source_path: str, database: str) -> None:
    control = parse_sql_loader(text)
    properties = json.loads(builder.nodes[sql_id]["properties_json"])
    properties.update({key: value for key, value in {"infile": control.infile, "badfile": control.badfile, "discardfile": control.discardfile}.items() if value})
    steps = [_loader_config_fact(control, source_path)]
    for target in control.targets:
        table_name = leaf_identifier(target.table)
        target_id = table_id(database, table_name)
        edge_type = "LOADS_INTO"
        if not catalog.has_table(database, table_name):
            target_id = unresolved_id(database, f"TABLE:{table_name}")
            builder.add_node(
                target_id,
                "UNRESOLVED_REFERENCE",
                table_name,
                f"{database}.{table_name}",
                table_name,
                database_key=database,
                graph_role="TECHNICAL",
                confidence=0.2,
                properties={
                    "database": database,
                    "table": table_name,
                    "raw_reference": target.table,
                    "loader": "SQL*Loader",
                },
            )
            edge_id = builder.add_edge(
                sql_id,
                target_id,
                edge_type,
                raw_operation=target.mode,
                confidence=0.5,
                properties={
                    "loader": "SQL*Loader",
                    "infile": control.infile,
                    "resolution": "unresolved_literal",
                },
            )
            builder.add_evidence(
                "EDGE",
                edge_id,
                source_path,
                target.line,
                target.line,
                "SQL_LOADER_CONTROL",
                line_text(text, target.line),
                confidence=0.5,
            )
            builder.add_issue("TABLE_NOT_IMPORTED", "ERROR", "SQL*Loader target table is absent from authoritative catalog", source_node_id=sql_id, raw_reference=table_name, database_key=database, source_path=source_path, start_line=target.line)
            ref_node_id = target_id
        else:
            edge_id = builder.add_edge(sql_id, target_id, edge_type, raw_operation=target.mode, properties={"loader": "SQL*Loader", "infile": control.infile})
            builder.add_evidence("EDGE", edge_id, source_path, target.line, target.line, "SQL_LOADER_CONTROL", line_text(text, target.line))
            ref_node_id = target_id
        fact = {
            "type": "data_effect",
            "label": f"{target.mode} {table_name}",
            "action": target.mode,
            "source": {"path": source_path, "line": target.line},
            "target": target.table,
            "resolution": "resolved" if ref_node_id else "unresolved",
            "condition": target.when,
            "trailing_nullcols": target.trailing_nullcols,
            "raw_clause": target.raw_clause,
            "steps": [_field_fact(field, source_path, database, table_name, catalog) for field in target.fields],
        }
        if ref_node_id:
            fact["ref_node_id"] = ref_node_id
        steps.append(fact)
        for field in target.fields:
            if field.filler:
                continue
            mapping = {key: value for key, value in {"sourceField": field.source_field, "targetColumn": field.column, "position": field.position, "datatype": field.datatype, "transform": field.transform, "constant": field.constant, "loadMode": target.mode}.items() if value}
            resolved_column = (
                catalog.has_table(database, table_name)
                and catalog.has_column(database, table_name, field.column)
            )
            if resolved_column:
                column_target = column_id(database, table_name, field.column)
                confidence = 1.0
            else:
                column_target = unresolved_id(
                    database, f"COLUMN:{table_name}:{field.column}"
                )
                owner = builder.nodes[sql_id]
                builder.add_node(
                    column_target,
                    "UNRESOLVED_REFERENCE",
                    field.column,
                    f"{database}.{table_name}.{field.column}",
                    field.column,
                    system_key=owner.get("system_key", ""),
                    database_key=database,
                    repository_key=owner.get("repository_key", ""),
                    graph_role="TECHNICAL",
                    confidence=0.2,
                    properties={
                        "database": database,
                        "table": table_name,
                        "column": field.column,
                        "loader": "SQL*Loader",
                    },
                )
                builder.add_issue("COLUMN_NOT_IMPORTED", "ERROR", "SQL*Loader target column is absent from authoritative catalog", source_node_id=sql_id, raw_reference=f"{table_name}.{field.column}", database_key=database, source_path=source_path, start_line=field.line)
                confidence = 0.5
                mapping["resolution"] = "unresolved_literal"
            edge_id = builder.add_edge(
                sql_id,
                column_target,
                "MAPS_TO",
                raw_operation="FIELD_MAP",
                confidence=confidence,
                properties=mapping,
            )
            builder.add_evidence(
                "EDGE",
                edge_id,
                source_path,
                field.line,
                field.line,
                "SQL_LOADER_CONTROL",
                line_text(text, field.line),
                confidence=confidence,
            )
    for line, message in control.errors:
        builder.add_issue("PARSE_ERROR", "ERROR", message, source_node_id=sql_id, raw_reference=line_text(text, line), database_key=database, source_path=source_path, start_line=line)
    properties["semantic_tree"] = {
        "version": 3,
        "type": "operation",
        "label": builder.nodes[sql_id]["technical_name"],
        "summary": f"Load {control.infile or 'runtime input'} into {len(control.targets)} table target(s).",
        "parameters": [],
        "steps": steps,
        "analysis_notes": analysis_notes(builder, sql_id, source_path, 1),
    }
    builder.nodes[sql_id]["properties_json"] = json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
