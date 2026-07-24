from __future__ import annotations

import json
import re
from dataclasses import dataclass

from contract.graph_contract import column_id, table_id
from extractors.package_support.package_writer import Catalog, PackageBuilder, leaf_identifier, line_text

_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$#]*)(?:\s*\.\s*(?:"(?:[^"]|"")+"|[A-Za-z_][\w$#]*))?'
_FILE_CLAUSE_RE = re.compile(r"\b(INFILE|BADFILE|DISCARDFILE)\s+(?:'((?:''|[^'])*)'|\"((?:\"\"|[^\"])*)\"|([^\s]+))", re.IGNORECASE)
_INTO_RE = re.compile(rf"\bINTO\s+TABLE\s+({_IDENTIFIER})", re.IGNORECASE)
_MODE_RE = re.compile(r"\b(INSERT|APPEND|REPLACE|TRUNCATE)\b", re.IGNORECASE)
_FIELD_RE = re.compile(r'^\s*("(?:[^"]|"")+"|[A-Za-z_][\w$#]*)(.*)$', re.DOTALL)


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


@dataclass(frozen=True)
class LoaderTarget:
    table: str
    mode: str
    line: int
    fields: tuple[LoaderField, ...]


@dataclass(frozen=True)
class LoaderControl:
    infile: str
    badfile: str
    discardfile: str
    targets: tuple[LoaderTarget, ...]
    errors: tuple[tuple[int, str], ...]


def parse_sql_loader(text: str) -> LoaderControl:
    """Parse the common SQL*Loader control-file subset without hiding unsupported syntax."""
    scan = _before_begindata(text)
    errors: list[tuple[int, str]] = []
    if not re.search(r"\bLOAD\s+DATA\b", scan, re.IGNORECASE):
        errors.append((1, "SQL*Loader control file is missing LOAD DATA"))

    files = {"INFILE": "", "BADFILE": "", "DISCARDFILE": ""}
    for match in _FILE_CLAUSE_RE.finditer(scan):
        files[match.group(1).upper()] = (match.group(2) or match.group(3) or match.group(4) or "").replace("''", "'").replace('""', '"')

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
        mode_match = _MODE_RE.search(segment[:_first_open_paren(segment)])
        mode = mode_match.group(1).upper() if mode_match else global_mode
        open_at = _first_open_paren(segment)
        fields: tuple[LoaderField, ...] = ()
        if open_at < len(segment):
            close_at = _matching_paren(segment, open_at)
            if close_at is None:
                errors.append((_line(scan, match.end() + open_at), f"Unclosed field list for {match.group(1)}"))
            else:
                block_start = match.end() + open_at + 1
                fields = tuple(_parse_fields(segment[open_at + 1:close_at], scan, block_start))
        targets.append(LoaderTarget(_clean_identifier(match.group(1)), mode, _line(scan, match.start()), fields))

    return LoaderControl(files["INFILE"], files["BADFILE"], files["DISCARDFILE"], tuple(targets), tuple(errors))


def _parse_fields(block: str, full_text: str, block_start: int) -> list[LoaderField]:
    fields: list[LoaderField] = []
    for raw, offset in _split_top_level(block):
        match = _FIELD_RE.match(raw)
        if not match:
            continue
        column = _clean_identifier(match.group(1))
        tail = " ".join(match.group(2).split())
        upper = tail.upper()
        filler = bool(re.search(r"\b(?:BOUND)?FILLER\b", upper))
        position_match = re.search(r"\bPOSITION\s*\(([^)]+)\)", tail, re.IGNORECASE)
        constant_match = re.search(r"\bCONSTANT\s+(?:'((?:''|[^'])*)'|([^\s,]+))", tail, re.IGNORECASE)
        quoted = re.findall(r'"((?:""|[^"])*)"', tail)
        transform = quoted[-1].replace('""', '"') if quoted and not constant_match else ""
        datatype_match = re.search(r"\b(INTEGER\s+EXTERNAL|DECIMAL\s+EXTERNAL|ZONED\s+DECIMAL|PACKED\s+DECIMAL|CHAR|DATE|TIMESTAMP|FLOAT|DOUBLE|RAW|VARRAW|VARCHAR|VARCHARC)\b", tail, re.IGNORECASE)
        constant = (constant_match.group(1) or constant_match.group(2) or "").replace("''", "'") if constant_match else ""
        fields.append(LoaderField(
            column=column,
            line=_line(full_text, block_start + offset + len(raw) - len(raw.lstrip())),
            source_field="" if constant else column,
            position=position_match.group(1).strip() if position_match else "",
            datatype=" ".join(datatype_match.group(1).upper().split()) if datatype_match else "",
            transform=transform,
            constant=constant,
            filler=filler,
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


def _first_open_paren(text: str) -> int:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            return index
    return len(text)


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


def extract_sql_loader(builder: PackageBuilder, catalog: Catalog, sql_id: str, text: str, source_path: str, database: str) -> None:
    control = parse_sql_loader(text)
    properties = json.loads(builder.nodes[sql_id]["properties_json"])
    properties.update({key: value for key, value in {"infile": control.infile, "badfile": control.badfile, "discardfile": control.discardfile}.items() if value})
    steps = []
    for target in control.targets:
        table_name = leaf_identifier(target.table)
        target_id = table_id(database, table_name)
        edge_type = "INSERTS" if target.mode in {"INSERT", "APPEND"} else "WRITES"
        if not catalog.has_table(database, table_name):
            builder.add_issue("TABLE_NOT_IMPORTED", "ERROR", "SQL*Loader target table is absent from authoritative catalog", source_node_id=sql_id, raw_reference=table_name, database_key=database, source_path=source_path, start_line=target.line)
            ref_node_id = ""
        else:
            edge_id = builder.add_edge(sql_id, target_id, edge_type, raw_operation=target.mode, properties={"loader": "SQL*Loader", "infile": control.infile})
            builder.add_evidence("EDGE", edge_id, source_path, target.line, target.line, "SQL_LOADER_CONTROL", line_text(text, target.line))
            ref_node_id = target_id
        fact = {"type": "data_effect", "label": f"{target.mode} {table_name}", "action": target.mode, "source": {"path": source_path, "line": target.line}}
        if ref_node_id:
            fact["ref_node_id"] = ref_node_id
        steps.append(fact)
        for field in target.fields:
            if field.filler or not catalog.has_table(database, table_name):
                continue
            mapping = {key: value for key, value in {"sourceField": field.source_field, "targetColumn": field.column, "position": field.position, "datatype": field.datatype, "transform": field.transform, "constant": field.constant, "loadMode": target.mode}.items() if value}
            if not catalog.has_column(database, table_name, field.column):
                builder.add_issue("COLUMN_NOT_IMPORTED", "ERROR", "SQL*Loader target column is absent from authoritative catalog", source_node_id=sql_id, raw_reference=f"{table_name}.{field.column}", database_key=database, source_path=source_path, start_line=field.line)
                continue
            edge_id = builder.add_edge(sql_id, column_id(database, table_name, field.column), "WRITES_COLUMN", raw_operation="FIELD_MAP", properties=mapping)
            builder.add_evidence("EDGE", edge_id, source_path, field.line, field.line, "SQL_LOADER_CONTROL", line_text(text, field.line))
    for line, message in control.errors:
        builder.add_issue("PARSE_ERROR", "ERROR", message, source_node_id=sql_id, raw_reference=line_text(text, line), database_key=database, source_path=source_path, start_line=line)
    properties["semantic_tree"] = {
        "version": 2,
        "type": "operation",
        "label": builder.nodes[sql_id]["technical_name"],
        "summary": f"Load {control.infile or 'runtime input'} into {len(control.targets)} table target(s).",
        "parameters": [],
        "steps": steps,
        "outputs": [],
        "exceptions": [],
        "analysis_notes": [{"type": "analysis_note", "code": issue["issue_type"], "severity": issue["severity"], "label": issue["message"]} for issue in builder.issues.values() if issue["source_node_id"] == sql_id],
    }
    builder.nodes[sql_id]["properties_json"] = json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
