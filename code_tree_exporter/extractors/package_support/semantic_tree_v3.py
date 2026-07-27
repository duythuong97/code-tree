from __future__ import annotations

from code_tree_exporter.extractors.package_support.oracle_parser import OraclePlsqlParser
from code_tree_exporter.extractors.package_support.package_writer import line_for_offset
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql


def plsql_steps(builder, owner_id: str, text: str, source_path: str, base_line: int) -> list[dict]:
    """Project ANTLR syntax into nested behavior facts; deliberately not a CFG."""
    return _PlsqlProjector(builder, owner_id, text, source_path, base_line).steps()


def sql_facts(builder, owner_id: str, text: str, source_path: str, base_line: int) -> list[dict]:
    facts = []
    for ref in analyze_sql(text).tables:
        target = _edge_target(builder, owner_id, ref.object_name, {ref.edge_type})
        fact = _fact(
            "data_effect",
            f"{ref.operation} {ref.object_name.upper()}",
            ref.start,
            text,
            source_path,
            base_line,
            action=ref.operation,
            resolution="resolved" if target else "unresolved",
        )
        if target:
            fact["ref_node_id"] = target
        facts.append(fact)
    return sorted(facts, key=lambda fact: (fact["source"]["line"], fact["type"], fact["label"]))


def analysis_notes(builder, owner_id: str, source_path: str, fallback_line: int) -> list[dict]:
    issues = [issue for issue in builder.issues.values() if issue["source_node_id"] == owner_id]
    return [
        {
            "type": "analysis_note",
            "code": issue["issue_type"],
            "severity": issue["severity"],
            "label": issue["message"],
            "source": {
                "path": issue.get("source_path") or source_path,
                "line": int(issue.get("start_line") or fallback_line),
            },
        }
        for issue in sorted(issues, key=lambda row: (int(row.get("start_line") or 0), row["issue_type"]))
    ]


class _PlsqlProjector:
    def __init__(self, builder, owner_id: str, text: str, source_path: str, base_line: int) -> None:
        self.builder = builder
        self.owner_id = owner_id
        self.text = text
        self.source_path = source_path
        self.base_line = base_line
        self.parser = OraclePlsqlParser(text)
        self.antlr = self.parser._antlr_parser
        self.calls = self.parser.calls()

    def steps(self) -> list[dict]:
        bodies = [node for node in self.parser._walk() if isinstance(node, self.antlr.BodyContext)]
        if not bodies:
            facts = sql_facts(self.builder, self.owner_id, self.text, self.source_path, self.base_line)
            return facts or [self._fact("statement", 0, _compact(self.text), expression=_compact(self.text), resolution="partial")]
        body = max(bodies, key=lambda node: node.stop.stop - node.start.start)
        return self._body(body)

    def _body(self, body) -> list[dict]:
        steps = self._declarations(body.parentCtx) + self._sequence(body.seq_of_statements())
        catches = [self._exception(item) for item in body.exception_handler()]
        if not catches:
            return steps
        return [self._fact("try", body.start.start, "BEGIN / EXCEPTION", steps=steps, catches=catches, finally_steps=[])]

    def _declarations(self, owner) -> list[dict]:
        sequence = owner.seq_of_declare_specs() if hasattr(owner, "seq_of_declare_specs") else None
        specs = sequence.declare_spec() if sequence else (owner.declare_spec() if hasattr(owner, "declare_spec") else [])
        facts = []
        for spec in specs:
            variable = spec.variable_declaration()
            if not variable or not variable.default_value_part():
                continue
            default = variable.default_value_part()
            target = self._text(variable.identifier())
            expression = self._text(default.expression())
            calls = self._calls(default.start.start, default.stop.stop + 1)
            facts.append(self._fact("assignment", default.start.start, f"Initialize {target}", target=target, expression=expression, action="INITIALIZE", **({"effects": calls} if calls else {})))
        return facts

    def _sequence(self, sequence) -> list[dict]:
        return [self._statement(statement) for statement in sequence.statement()]

    def _statement(self, statement) -> dict:
        if statement.if_statement():
            return self._if(statement.if_statement())
        if statement.loop_statement():
            node = statement.loop_statement()
            iterator = self._text(node.cursor_loop_param()) if node.cursor_loop_param() else ""
            condition = self._text(node.condition()) if node.condition() else ""
            return self._fact("loop", node.start.start, _compact(iterator or condition or "LOOP"), condition=condition, iterator=iterator, steps=self._sequence(node.seq_of_statements()))
        if statement.case_statement():
            return self._case(statement.case_statement())
        if statement.assignment_statement():
            node = statement.assignment_statement()
            target = self._text(node.general_element() or node.bind_variable())
            expression = self._text(node.expression())
            calls = self._calls(node.start.start, node.stop.stop + 1)
            return self._fact("assignment", node.start.start, f"Set {target}", target=target, expression=expression, action=":=", **({"effects": calls} if calls else {}))
        if statement.return_statement():
            node = statement.return_statement()
            expression = self._text(node.expression()) if node.expression() else ""
            return self._fact("output", node.start.start, f"Return {_compact(expression)}".rstrip(), expression=expression, action="RETURN")
        if statement.raise_statement():
            node = statement.raise_statement()
            expression = self._text(node.exception_name()) if node.exception_name() else ""
            return self._fact("raise", node.start.start, f"Raise {expression}".rstrip(), expression=expression, action="RAISE")
        if statement.continue_statement():
            node = statement.continue_statement()
            condition = self._text(node.condition()) if node.condition() else ""
            return self._fact("continue", node.start.start, "Continue", condition=condition)
        if statement.exit_statement():
            node = statement.exit_statement()
            condition = self._text(node.condition()) if node.condition() else ""
            return self._fact("exit", node.start.start, "Exit loop", condition=condition)
        if statement.sql_statement():
            return self._sql(statement.sql_statement())
        if statement.call_statement():
            calls = self._calls(statement.start.start, statement.stop.stop + 1)
            if len(calls) == 1:
                return calls[0]
            return self._fact("statement", statement.start.start, _compact(self._text(statement)), expression=_compact(self._text(statement)), effects=calls, resolution="partial")
        nested = statement.body() or (statement.block().body() if statement.block() else None)
        if nested:
            return self._fact("block", statement.start.start, "Nested block", steps=self._body(nested))
        return self._fact("statement", statement.start.start, _compact(self._text(statement)), expression=_compact(self._text(statement)), resolution="partial")

    def _if(self, node) -> dict:
        else_steps = self._sequence(node.else_part().seq_of_statements()) if node.else_part() else []
        for part in reversed(node.elsif_part()):
            condition = self._text(part.condition())
            else_steps = [self._fact("branch", part.start.start, f"ELSIF {_compact(condition)}", condition=condition, steps=self._sequence(part.seq_of_statements()), else_steps=else_steps)]
        condition = self._text(node.condition())
        return self._fact("branch", node.start.start, f"IF {_compact(condition)}", condition=condition, steps=self._sequence(node.seq_of_statements()), else_steps=else_steps)

    def _case(self, node) -> dict:
        case = node.simple_case_statement() or node.searched_case_statement()
        expression = self._text(case.expression()) if hasattr(case, "expression") and case.expression() else ""
        cases = []
        for part in case.case_when_part_statement():
            condition = self._text(part.expression())
            cases.append(self._fact("case", part.start.start, f"WHEN {_compact(condition)}", condition=condition, steps=self._sequence(part.seq_of_statements())))
        otherwise = case.case_else_part_statement()
        return self._fact("branch", case.start.start, f"CASE {_compact(expression)}".rstrip(), expression=expression, cases=cases, else_steps=self._sequence(otherwise.seq_of_statements()) if otherwise else [], steps=[])

    def _exception(self, node) -> dict:
        names = [self._text(name) for name in node.exception_name()]
        condition = " OR ".join(names)
        return self._fact("catch", node.start.start, condition, condition=condition, steps=self._sequence(node.seq_of_statements()))

    def _sql(self, node) -> dict:
        raw = self._text(node)
        facts = sql_facts(self.builder, self.owner_id, raw, self.source_path, self.base_line + line_for_offset(self.text, node.start.start) - 1)
        facts.extend(self._calls(node.start.start, node.stop.stop + 1))
        if len(facts) == 1:
            return facts[0]
        action = raw.split(None, 1)[0].upper() if raw.split() else "SQL"
        return self._fact("sql", node.start.start, _compact(raw), action=action, expression=_compact(raw), effects=facts)

    def _calls(self, start: int, end: int) -> list[dict]:
        facts = []
        for call in self.calls:
            if not start <= call.start < end:
                continue
            target = _edge_target(self.builder, self.owner_id, call.object_name, {"CALLS", "CALLS_API"})
            fact = self._fact("call", call.start, f"Call {call.object_name}", arguments=_call_arguments(self.text[call.start:end]), resolution="resolved" if target else "unresolved")
            if target:
                fact["ref_node_id"] = target
            facts.append(fact)
        return facts

    def _fact(self, kind: str, offset: int, label: str, **extra) -> dict:
        return _fact(kind, label, offset, self.text, self.source_path, self.base_line, **extra)

    def _text(self, node) -> str:
        return self.parser._source(node) if node is not None else ""


def _edge_target(builder, owner_id: str, object_name: str, edge_types: set[str]) -> str | None:
    leaf = object_name.upper().split("@", 1)[0].rsplit(".", 1)[-1]
    candidates = []
    for edge in builder.edges.values():
        if edge["source_node_id"] != owner_id or edge["edge_type"] not in edge_types:
            continue
        target = builder.nodes.get(edge["target_node_id"], {})
        technical = (target.get("technical_name") or "").upper()
        if technical in {leaf, object_name.upper()}:
            candidates.append(edge["target_node_id"])
    unique = set(candidates)
    return next(iter(unique)) if len(unique) == 1 else None


def _call_arguments(raw: str) -> list[str]:
    left = raw.find("(")
    if left < 0:
        return []
    depth = 0
    quote = False
    for index, char in enumerate(raw[left:], left):
        if char == "'":
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")":
            depth -= 1
            if depth == 0:
                return [_compact(value) for value in _split_top_level(raw[left + 1:index]) if value.strip()]
    return []


def _split_top_level(value: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, False
    for index, char in enumerate(value):
        if char == "'":
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")":
            depth = max(0, depth - 1)
        elif not quote and char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _fact(kind: str, label: str, offset: int, text: str, source_path: str, base_line: int, **extra) -> dict:
    return {
        "type": kind,
        "label": label,
        "source": {"path": source_path, "line": base_line + line_for_offset(text, offset) - 1},
        **extra,
    }


def _compact(value: str) -> str:
    return " ".join(value.split())[:180]
