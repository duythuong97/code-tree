from __future__ import annotations

import codecs
import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_ENCODING_DECLARATION = re.compile(
    rb"(?i)(?:coding\s*[:=]|charset\s*=|encoding\s*=)\s*['\"]?([a-z0-9._-]+)"
)

SUPPORTED_ENCODINGS = frozenset({
    "utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "shift_jis", "cp932", "euc_jp"
})
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "UTF-16-LE"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "UTF-16-BE"),
)


class SourceDecodingError(ValueError):
    def __init__(self, issue_type: str, path: str, message: str, properties: dict[str, object]) -> None:
        super().__init__(message)
        self.issue_type = issue_type
        self.path = path
        self.properties = properties


@dataclass(frozen=True)
class DecodedSource:
    path: Path
    relative_path: str
    raw_bytes: bytes
    text: str
    declared_encoding: str
    actual_encoding: str
    raw_sha256: str
    text_sha256: str
    newline_style: str
    bom: str


def canonical_encoding(value: str) -> str:
    if value.strip().lower() == "auto":
        return "auto"
    name = codecs.lookup(value).name
    aliases = {"utf-8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be", "shift-jis": "shift_jis", "cp932": "cp932", "euc-jp": "euc_jp"}
    canonical = aliases.get(name, name)
    if canonical not in SUPPORTED_ENCODINGS:
        raise ValueError(f"Unsupported source encoding: {value}")
    return canonical


def encoding_for(relative_path: str, source: dict, default_encoding: str) -> str:
    selected = source.get("encoding") or default_encoding
    for override in source.get("encodingOverrides", []):
        if fnmatch.fnmatchcase(relative_path, str(override.get("glob", ""))):
            selected = override.get("encoding", selected)
    return canonical_encoding(str(selected))


def decode_source(path: Path, relative_path: str, declared_encoding: str) -> DecodedSource:
    raw = path.read_bytes()
    declared = canonical_encoding(declared_encoding)
    bom_codec, bom_name = _bom(raw)
    file_declaration = _declared_in_file(raw, relative_path)
    if bom_codec and declared != "auto" and not _compatible_bom(declared, bom_codec):
        raise SourceDecodingError(
            "ENCODING_CONFLICT",
            relative_path,
            f"BOM {bom_name} conflicts with configured encoding {declared}",
            {"configured_encoding": declared, "bom": bom_name},
        )
    if bom_codec and file_declaration and not _compatible_bom(file_declaration, bom_codec):
        raise SourceDecodingError(
            "ENCODING_CONFLICT",
            relative_path,
            f"BOM {bom_name} conflicts with file declaration {file_declaration}",
            {"declared_encoding": file_declaration, "bom": bom_name},
        )
    actual = bom_codec or (declared if declared != "auto" else file_declaration) or _detect_encoding(raw, relative_path)
    try:
        text = raw.decode(actual, errors="strict")
    except UnicodeDecodeError as exc:
        start = max(0, exc.start - 8)
        end = min(len(raw), exc.end + 8)
        raise SourceDecodingError(
            "ENCODING_ERROR",
            relative_path,
            f"Cannot decode source as {actual} at byte {exc.start}",
            {"encoding": actual, "byte_offset": exc.start, "hex_context": raw[start:end].hex()},
        ) from exc
    if bom_codec and text.startswith("\ufeff"):
        text = text[1:]
    return DecodedSource(
        path=path,
        relative_path=relative_path,
        raw_bytes=raw,
        text=text,
        declared_encoding=declared,
        actual_encoding=actual,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        newline_style=_newline_style(text),
        bom=bom_name,
    )


def _bom(raw: bytes) -> tuple[str, str]:
    for marker, codec, name in _BOMS:
        if raw.startswith(marker):
            return codec, name
    return "", ""

def _declared_in_file(raw: bytes, relative_path: str) -> str:
    match = _ENCODING_DECLARATION.search(raw[:4096])
    if not match:
        return ""
    try:
        return canonical_encoding(match.group(1).decode("ascii"))
    except (LookupError, ValueError) as exc:
        value = match.group(1).decode("ascii", errors="backslashreplace")
        raise SourceDecodingError(
            "ENCODING_ERROR", relative_path, f"Unsupported encoding declaration: {value}",
            {"declared_encoding": value},
        ) from exc

def _detect_encoding(raw: bytes, relative_path: str) -> str:
    if not raw:
        return "utf-8"
    try:
        raw.decode("utf-8", errors="strict")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    if _looks_utf16(raw, little_endian=True):
        return "utf-16-le"
    if _looks_utf16(raw, little_endian=False):
        return "utf-16-be"
    matches = []
    for encoding in ("cp932", "euc_jp"):
        try:
            matches.append((encoding, raw.decode(encoding, errors="strict")))
        except UnicodeDecodeError:
            pass
    if len(matches) == 1 or len(matches) == 2 and matches[0][1] == matches[1][1]:
        return matches[0][0]
    if len(matches) > 1:
        ranked = sorted(matches, key=lambda item: _mojibake_score(item[1]))
        if _mojibake_score(ranked[0][1]) < _mojibake_score(ranked[1][1]):
            return ranked[0][0]
        raise SourceDecodingError(
            "ENCODING_CONFLICT", relative_path, "Legacy Japanese encoding is ambiguous",
            {"candidate_encodings": [encoding for encoding, _ in matches]},
        )
    raise SourceDecodingError(
        "ENCODING_ERROR", relative_path, "Cannot detect source encoding",
        {"hex_context": raw[:16].hex()},
    )

def _mojibake_score(text: str) -> int:
    return sum(
        1 for character in text
        if "\ue000" <= character <= "\uf8ff" or "\uff61" <= character <= "\uff9f"
    )

def _looks_utf16(raw: bytes, *, little_endian: bool) -> bool:
    pairs = len(raw) // 2
    if pairs < 2:
        return False
    zero_side = raw[1::2] if little_endian else raw[0::2]
    text_side = raw[0::2] if little_endian else raw[1::2]
    return zero_side.count(0) / pairs > 0.3 and text_side.count(0) / pairs < 0.1

def _compatible_bom(declared: str, bom_codec: str) -> bool:
    return declared == bom_codec or (bom_codec == "utf-8-sig" and declared in {"utf-8", "utf-8-sig"})


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    remainder = text.replace("\r\n", "")
    lf = remainder.count("\n")
    cr = remainder.count("\r")
    kinds = sum(value > 0 for value in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "CRLF"
    if lf:
        return "LF"
    if cr:
        return "CR"
    return "none"
