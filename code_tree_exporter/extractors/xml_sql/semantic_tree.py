from __future__ import annotations

from xml.etree import ElementTree
from xml.parsers import expat


def element_lines(text: str, root: ElementTree.Element) -> dict[int, int]:
    lines: list[int] = []
    parser = expat.ParserCreate()
    parser.StartElementHandler = lambda _name, _attrs: lines.append(parser.CurrentLineNumber)
    parser.Parse(text, True)
    return {id(element): line for element, line in zip(root.iter(), lines)}


def statement_semantic_tree(
    statement: ElementTree.Element,
    *,
    query_id: str,
    source_path: str,
    line_by_element: dict[int, int],
    fragments: dict[str, tuple[str, ElementTree.Element]],
    analysis_notes: list[dict],
) -> dict:
    projector = _Projector(source_path, line_by_element, fragments)
    return {
        "version": 3,
        "type": "operation",
        "label": query_id,
        "summary": "",
        "parameters": [],
        "steps": projector.mixed(statement),
        "analysis_notes": analysis_notes,
    }


class _Projector:
    def __init__(
        self,
        source_path: str,
        line_by_element: dict[int, int],
        fragments: dict[str, tuple[str, ElementTree.Element]],
    ) -> None:
        self.source_path = source_path
        self.line_by_element = line_by_element
        self.fragments = fragments

    def mixed(self, element: ElementTree.Element, active: tuple[str, ...] = ()) -> list[dict]:
        steps: list[dict] = []
        self._text(steps, element.text, element)
        for child in element:
            steps.append(self._element(child, active))
            self._text(steps, child.tail, child)
        return steps

    def _element(self, element: ElementTree.Element, active: tuple[str, ...]) -> dict:
        tag = _tag(element)
        source = self._source(element)
        if tag == "if":
            return {"type": "branch", "label": "if", "source": source, "condition": element.get("test", ""), "steps": self.mixed(element, active), "else_steps": []}
        if tag == "choose":
            cases, else_steps = [], []
            for child in element:
                child_tag = _tag(child)
                if child_tag == "when":
                    cases.append({"type": "case", "label": "when", "source": self._source(child), "condition": child.get("test", ""), "steps": self.mixed(child, active)})
                elif child_tag == "otherwise":
                    else_steps.append({"type": "case", "label": "otherwise", "source": self._source(child), "steps": self.mixed(child, active)})
                else:
                    cases.append(self._element(child, active))
            return {"type": "branch", "label": "choose", "source": source, "cases": cases, "else_steps": else_steps}
        if tag == "foreach":
            return {
                "type": "loop",
                "label": "foreach",
                "source": source,
                "iterator": element.get("item", ""),
                "expression": element.get("collection", ""),
                "index": element.get("index", ""),
                "open": element.get("open", ""),
                "close": element.get("close", ""),
                "separator": element.get("separator", ""),
                "steps": self.mixed(element, active),
            }
        if tag in {"trim", "where", "set"}:
            return {
                "type": "sql_scope",
                "label": tag,
                "source": source,
                "action": tag.upper(),
                **{key: value for key, value in element.attrib.items()},
                "steps": self.mixed(element, active),
            }
        if tag == "bind":
            return {"type": "assignment", "label": "bind", "source": source, "target": element.get("name", ""), "expression": element.get("value", "")}
        if tag == "include":
            return self._include(element, active)
        if tag == "property":
            return {"type": "argument", "label": element.get("name", ""), "source": source, "expression": element.get("value", "")}
        return {
            "type": "statement",
            "label": tag,
            "source": source,
            "resolution": "partial",
            **{key: value for key, value in element.attrib.items()},
            "steps": self.mixed(element, active),
        }

    def _include(self, element: ElementTree.Element, active: tuple[str, ...]) -> dict:
        refid = element.get("refid", "").strip()
        fact = {
            "type": "include",
            "label": "include",
            "source": self._source(element),
            "target": refid,
            "arguments": [f"{child.get('name', '')}={child.get('value', '')}" for child in element if _tag(child) == "property"],
            "steps": [],
        }
        if "${" in refid or "#{" in refid or refid not in self.fragments:
            fact["resolution"] = "unresolved"
            return fact
        canonical, fragment = self.fragments[refid]
        if canonical in active:
            fact["resolution"] = "partial"
            fact["cycle"] = list(active + (canonical,))
            return fact
        fact["resolution"] = "partial" if fact["arguments"] else "resolved"
        fact["steps"] = self.mixed(fragment, active + (canonical,))
        return fact

    def _text(self, steps: list[dict], value: str | None, owner: ElementTree.Element) -> None:
        text = " ".join((value or "").split())
        if text:
            steps.append({"type": "sql", "label": "SQL", "source": self._source(owner), "expression": text})

    def _source(self, element: ElementTree.Element) -> dict:
        return {"path": self.source_path, "line": self.line_by_element.get(id(element), 1)}


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()
