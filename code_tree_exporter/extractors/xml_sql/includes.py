from __future__ import annotations

import re
from xml.etree import ElementTree

IncludeIssue = tuple[str, str, str, str, tuple[str, ...]]


def expanded_sql(
    element: ElementTree.Element,
    fragments: dict[str, tuple[str, ElementTree.Element]],
    active: tuple[str, ...] = (),
) -> tuple[str, list[IncludeIssue]]:
    parts = [element.text or ""]
    issues: list[IncludeIssue] = []
    for child in element:
        if _tag(child) == "include":
            refid = child.get("refid", "").strip()
            if re.search(r"[$#]\{[^}]*\}", refid):
                issues.append((
                    "DYNAMIC_CONFIG_KEY", "WARNING",
                    "Runtime XML SQL include cannot be resolved", refid, active,
                ))
            elif refid not in fragments:
                issues.append((
                    "INVALID_CONFIG", "ERROR",
                    "XML SQL include fragment not found", refid, active,
                ))
            else:
                canonical, fragment = fragments[refid]
                if canonical in active:
                    chain = active + (canonical,)
                    issues.append((
                        "INVALID_CONFIG", "ERROR",
                        f"Cyclic XML SQL include: {' -> '.join(chain)}", refid, chain,
                    ))
                else:
                    nested_sql, nested_issues = expanded_sql(
                        fragment, fragments, active + (canonical,)
                    )
                    parts.append(nested_sql)
                    issues.extend(nested_issues)
        else:
            nested_sql, nested_issues = expanded_sql(child, fragments, active)
            parts.append(nested_sql)
            issues.extend(nested_issues)
        parts.append(child.tail or "")
    return " ".join(parts), issues


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()
