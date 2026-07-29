from __future__ import annotations

from collections.abc import Iterator
from xml.etree import ElementTree


def local_tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def statement_elements(root: ElementTree.Element) -> Iterator[ElementTree.Element]:
    """Yield addressable XML units without interpreting vendor statement tags."""

    fragments = {
        id(element)
        for element in root.iter()
        if local_tag(element) == "sql" and element.get("id", "").strip()
    }
    for element in root.iter():
        if element.get("id", "").strip() and id(element) not in fragments:
            yield element
