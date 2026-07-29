from __future__ import annotations

from dataclasses import dataclass
import re

OP_TO_EDGE = {
    "SELECT": "READS_FROM",
    "INSERT": "WRITES_TO",
    "UPDATE": "WRITES_TO",
    "DELETE": "WRITES_TO",
    "MERGE": "WRITES_TO",
}

@dataclass(frozen=True)
class ParsedSqlReference:
    object_name: str
    operation: str
    relation: str
    start: int
    db_link: str = ""

@dataclass(frozen=True)
class ParsedCallReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class ParsedSequenceReference:
    object_name: str
    operation: str
    start: int

@dataclass(frozen=True)
class ParsedRoutineDeclaration:
    kind: str
    name: str
    parameter_block: str | None
    start: int
    end: int

@dataclass(frozen=True)
class ParsedTriggerDeclaration:
    name: str
    table_name: str
    start: int
    end: int

@dataclass(frozen=True)
class ParsedSynonymDeclaration:
    name: str
    target_name: str
    start: int

@dataclass(frozen=True)
class ParsedViewDeclaration:
    kind: str
    name: str
    start: int
    body_start: int
    end: int

def mask_noncode(text: str) -> str:
    """Replace comments and string literals with spaces while preserving offsets."""
    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        pair = text[index:index + 2]
        if state == "code":
            if text[index] in "nN" and index + 1 < len(text) and text[index + 1] == "'":
                chars[index:index + 2] = [" ", " "]
                index += 2
                state = "string"
                continue
            if text[index] in "qQ" and index + 2 < len(text) and text[index + 1] == "'":
                opening = text[index + 2]
                closing = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opening, opening)
                end = text.find(closing + "'", index + 3)
                if end >= 0:
                    for offset in range(index, end + 2):
                        if chars[offset] != "\n":
                            chars[offset] = " "
                    index = end + 2
                    continue
            if pair == "--":
                state = "line_comment"
                chars[index:index + 2] = [" ", " "]
                index += 2
                continue
            if pair == "/*":
                state = "block_comment"
                chars[index:index + 2] = [" ", " "]
                index += 2
                continue
            if text[index] == "'":
                state = "string"
                chars[index] = " "
            index += 1
            continue
        if state == "line_comment":
            if text[index] == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if pair == "*/":
                chars[index:index + 2] = [" ", " "]
                state = "code"
                index += 2
            else:
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if text[index] == "'":
            chars[index] = " "
            if index + 1 < len(text) and text[index + 1] == "'":
                chars[index + 1] = " "
                index += 2
            else:
                state = "code"
                index += 1
        else:
            if text[index] != "\n":
                chars[index] = " "
            index += 1
    return "".join(chars)

class OraclePlsqlParser:
    """Facade over the vendored grammars-v4 Oracle PL/SQL parser.

    The source is parsed exactly once. ANTLR recovery errors are exposed through
    ``syntax_errors``; no structural fallback is attempted.
    """

    def __init__(self, text: str):
        from antlr4 import CommonTokenStream, InputStream
        from antlr4.error.ErrorListener import ErrorListener
        from code_tree_exporter.extractors.package_support.antlr_plsql_generated.PlSqlLexer import PlSqlLexer
        from code_tree_exporter.extractors.package_support.antlr_plsql_generated.PlSqlParser import PlSqlParser

        class _ErrorCollector(ErrorListener):
            def __init__(self) -> None:
                self.errors: list[tuple[int, int, str]] = []

            def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e) -> None:
                self.errors.append((line, column, msg))

        self.text = text
        listener = _ErrorCollector()
        lexer = PlSqlLexer(InputStream(text))
        lexer.removeErrorListeners()
        lexer.addErrorListener(listener)
        stream = CommonTokenStream(lexer)
        parser = PlSqlParser(stream)
        parser.removeErrorListeners()
        parser.addErrorListener(listener)
        self._antlr_parser = parser
        self._tree = parser.sql_script()
        self.syntax_errors = tuple(listener.errors)

    def package_name(self) -> str | None:
        parser = self._antlr_parser
        for node in self._walk():
            if isinstance(node, (parser.Create_packageContext, parser.Create_package_bodyContext)):
                names = node.package_name()
                if names:
                    return self._identifier(names[0]).upper()
        return None

    def routines(self) -> list[ParsedRoutineDeclaration]:
        parser = self._antlr_parser
        context_specs = (
            (parser.Create_procedure_bodyContext, "PROCEDURE", "procedure_name"),
            (parser.Create_function_bodyContext, "FUNCTION", "function_name"),
            (parser.Procedure_bodyContext, "PROCEDURE", "identifier"),
            (parser.Function_bodyContext, "FUNCTION", "identifier"),
            (parser.Procedure_specContext, "PROCEDURE", "identifier"),
            (parser.Function_specContext, "FUNCTION", "identifier"),
        )
        routines: list[ParsedRoutineDeclaration] = []
        for node in self._walk():
            for context_type, kind, accessor in context_specs:
                if not isinstance(node, context_type):
                    continue
                name_node = getattr(node, accessor)()
                name = self._identifier(name_node).rsplit(".", 1)[-1].upper()
                routines.append(
                    ParsedRoutineDeclaration(
                        kind,
                        name,
                        self._parameter_block_from_context(node),
                        node.start.start,
                        node.stop.stop + 1,
                    )
                )
                break
        return sorted(routines, key=lambda item: (item.start, item.end))

    def triggers(self) -> list[ParsedTriggerDeclaration]:
        parser = self._antlr_parser
        triggers: list[ParsedTriggerDeclaration] = []
        for node in self._walk():
            if not isinstance(node, parser.Create_triggerContext):
                continue
            table = next(
                (
                    self._source(child)
                    for child in self._walk(node)
                    if isinstance(child, parser.Tableview_nameContext)
                ),
                "",
            )
            if table:
                triggers.append(
                    ParsedTriggerDeclaration(
                        self._identifier(node.trigger_name()).rsplit(".", 1)[-1].upper(),
                        self._normalize_name(table),
                        node.start.start,
                        node.stop.stop + 1,
                    )
                )
        return triggers

    def synonyms(self) -> list[ParsedSynonymDeclaration]:
        parser = self._antlr_parser
        synonyms: list[ParsedSynonymDeclaration] = []
        for node in self._walk():
            if not isinstance(node, parser.Create_synonymContext):
                continue
            name_node = node.synonym_name()
            object_node = node.schema_object_name()
            if name_node is None or object_node is None:
                continue
            target_parts = [self._source(item) for item in node.schema_name()]
            target_parts.append(self._source(object_node))
            target = ".".join(part for part in target_parts if part)
            link_node = node.link_name()
            if link_node is not None:
                target += "@" + self._source(link_node)
            synonyms.append(
                ParsedSynonymDeclaration(
                    self._identifier(name_node),
                    self._normalize_name(target),
                    name_node.start.start,
                )
            )
        return synonyms

    def views(self) -> list[ParsedViewDeclaration]:
        parser = self._antlr_parser
        views: list[ParsedViewDeclaration] = []
        for node in self._walk():
            if isinstance(node, parser.Create_viewContext):
                name_parts = []
                schema = node.schema_name()
                if schema is not None:
                    name_parts.append(self._source(schema))
                name_parts.append(self._source(node.v))
                body = node.select_only_statement()
                kind = "VIEW"
            elif isinstance(node, parser.Create_materialized_viewContext):
                name_parts = [self._source(node.tableview_name())]
                body = node.select_only_statement()
                kind = "MATERIALIZED_VIEW"
            else:
                continue
            views.append(
                ParsedViewDeclaration(
                    kind,
                    self._normalize_name(".".join(name_parts)),
                    node.start.start,
                    body.start.start,
                    node.stop.stop + 1,
                )
            )
        return sorted(views, key=lambda item: item.start)

    def script_classification(self) -> str:
        parser = self._antlr_parser
        plsql_contexts = (
            parser.Anonymous_blockContext,
            parser.Create_procedure_bodyContext,
            parser.Create_function_bodyContext,
            parser.Create_packageContext,
            parser.Create_package_bodyContext,
            parser.Create_triggerContext,
            parser.Create_viewContext,
            parser.Create_materialized_viewContext,
            parser.Create_synonymContext,
        )
        has_plsql = False
        has_dml = False
        for node in self._walk():
            has_plsql = has_plsql or isinstance(node, plsql_contexts)
            has_dml = has_dml or isinstance(
                node, parser.Data_manipulation_language_statementsContext
            )
        if has_plsql and has_dml:
            return "MIXED_SCRIPT"
        if has_plsql:
            return "PLSQL_DEFINITION"
        if has_dml:
            return "DML_SCRIPT"
        return "UNKNOWN_SQL"

    def table_references(self) -> list[ParsedSqlReference]:
        """Return table references classified entirely by grammar ancestry."""

        parser = self._antlr_parser
        cte_names = {
            self._normalize_name(self._source(node)).upper()
            for node in self._walk()
            if isinstance(node, parser.Query_nameContext)
        }
        references: list[ParsedSqlReference] = []
        seen: set[tuple[str, str]] = set()
        for node in self._walk():
            if not isinstance(node, parser.Tableview_nameContext):
                continue
            parts = [self._identifier(node.identifier())]
            if node.id_expression() is not None:
                parts.append(self._identifier(node.id_expression()))
            name = ".".join(part for part in parts if part)
            db_link = self._identifier(node.link_name()).upper() if node.link_name() else ""
            if not name or name.upper() in cte_names:
                continue
            operation, relation = "SELECT", OP_TO_EDGE["SELECT"]
            ancestor = node.parentCtx
            while ancestor is not None:
                if isinstance(ancestor, parser.Select_statementContext):
                    break
                if isinstance(ancestor, parser.Update_statementContext):
                    operation, relation = "UPDATE", OP_TO_EDGE["UPDATE"]
                    break
                if isinstance(ancestor, parser.Delete_statementContext):
                    operation, relation = "DELETE", OP_TO_EDGE["DELETE"]
                    break
                if isinstance(ancestor, parser.Insert_into_clauseContext):
                    operation, relation = "INSERT", OP_TO_EDGE["INSERT"]
                    break
                if isinstance(ancestor, parser.Selected_tableviewContext) and isinstance(
                    ancestor.parentCtx, parser.Merge_statementContext
                ):
                    selected = ancestor.parentCtx.selected_tableview()
                    if selected and ancestor is selected[0]:
                        operation, relation = "MERGE", OP_TO_EDGE["MERGE"]
                    break
                ancestor = ancestor.parentCtx
            key = (name.upper(), relation)
            if key not in seen:
                seen.add(key)
                references.append(ParsedSqlReference(name, operation, relation, node.start.start, db_link))
        return sorted(references, key=lambda item: (item.start, item.relation, item.object_name))

    def sequences(self) -> list[ParsedSequenceReference]:
        """Return NEXTVAL/CURRVAL references from parsed qualified expressions."""

        parser = self._antlr_parser
        references: list[ParsedSequenceReference] = []
        seen: set[tuple[str, int]] = set()
        for node in self._walk():
            if not isinstance(node, parser.General_elementContext):
                continue
            parts = [self._normalize_name(part) for part in self._source(node).split(".")]
            if len(parts) < 2:
                continue
            operation = parts[-1].upper()
            if operation not in {"NEXTVAL", "CURRVAL"}:
                continue
            name = ".".join(parts[:-1])
            key = (name.upper(), node.start.start)
            if name and key not in seen:
                seen.add(key)
                references.append(ParsedSequenceReference(name, operation, node.start.start))
        return sorted(references, key=lambda item: (item.start, item.object_name))

    def routine_parameters(self, routine: ParsedRoutineDeclaration) -> list[dict[str, str]]:
        """Return parameters owned by one parsed routine declaration."""

        parser = self._antlr_parser
        result: list[dict[str, str]] = []
        for node in self._walk():
            if not isinstance(node, parser.ParameterContext):
                continue
            if node.start.start < routine.start or node.stop.stop >= routine.end:
                continue
            owner = node.parentCtx
            while owner is not None and not isinstance(
                owner,
                (
                    parser.Create_procedure_bodyContext,
                    parser.Create_function_bodyContext,
                    parser.Procedure_bodyContext,
                    parser.Function_bodyContext,
                    parser.Procedure_specContext,
                    parser.Function_specContext,
                ),
            ):
                owner = owner.parentCtx
            if owner is None or owner.start.start != routine.start:
                continue
            name_node = node.parameter_name()
            type_node = node.type_spec()
            direction = "IN OUT" if node.INOUT() else "OUT" if node.OUT() else "IN"
            result.append(
                {
                    "name": self._source(name_node) if name_node else "",
                    "direction": direction,
                    "data_type": self._source(type_node) if type_node else "",
                    "raw": self._source(node),
                }
            )
        return result

    def routine_signature(self, routine: ParsedRoutineDeclaration) -> str:
        data_types = [
            parameter["data_type"].upper()
            for parameter in self.routine_parameters(routine)
            if parameter["data_type"]
        ]
        return "_".join(data_types) if data_types else "void"

    def calls(self) -> list[ParsedCallReference]:
        parser = self._antlr_parser
        calls: list[ParsedCallReference] = []
        seen: set[tuple[int, str]] = set()
        for node in self._walk():
            raw = ""
            start = node.start.start
            if isinstance(node, parser.Call_statementContext):
                names = node.routine_name()
                if names:
                    raw = ".".join(self._identifier(name) for name in names)
                    start = names[0].start.start
            elif isinstance(node, parser.General_elementContext):
                arguments = [
                    child
                    for child in self._walk(node)
                    if isinstance(child, parser.Function_argumentContext)
                ]
                if arguments:
                    raw = self.text[node.start.start : arguments[0].start.start]
            if not raw:
                continue
            name = self._normalize_name(raw)
            key = (start, name.upper())
            if name and key not in seen:
                seen.add(key)
                calls.append(ParsedCallReference(name, start))
        return sorted(calls, key=lambda item: (item.start, item.object_name))

    def dynamic_sql_offsets(self) -> list[int]:
        parser = self._antlr_parser
        return [
            node.start.start
            for node in self._walk()
            if isinstance(node, parser.Execute_immediateContext)
        ]

    def has_executable_statement(self) -> bool:
        parser = self._antlr_parser
        executable_contexts = (
            parser.Data_manipulation_language_statementsContext,
            parser.Anonymous_blockContext,
            parser.Create_procedure_bodyContext,
            parser.Create_function_bodyContext,
            parser.Create_packageContext,
            parser.Create_package_bodyContext,
            parser.Create_triggerContext,
            parser.Create_viewContext,
            parser.Create_materialized_viewContext,
            parser.Execute_immediateContext,
        )
        for node in self._walk():
            if isinstance(node, executable_contexts):
                return True
            if isinstance(node, parser.Call_statementContext) and node.CALL() is not None:
                return True
        return False

    def _walk(self, root=None):
        node = self._tree if root is None else root
        yield node
        for child in getattr(node, "children", ()) or ():
            if hasattr(child, "getRuleIndex"):
                yield from self._walk(child)

    def _source(self, node) -> str:
        return self.text[node.start.start : node.stop.stop + 1]

    def _identifier(self, node) -> str:
        return self._normalize_name(self._source(node))

    @staticmethod
    def _normalize_name(raw: str) -> str:
        return re.sub(r"\s*\.\s*", ".", raw.strip()).replace('"', "")

    def _parameter_block_from_context(self, node) -> str | None:
        left = node.LEFT_PAREN() if hasattr(node, "LEFT_PAREN") else None
        right = node.RIGHT_PAREN() if hasattr(node, "RIGHT_PAREN") else None
        if not left or not right:
            return None
        return self.text[left.symbol.start : right.symbol.stop + 1]
