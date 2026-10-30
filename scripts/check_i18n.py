#!/usr/bin/env python3
"""Fail when a string the GUI asks for is missing from ``en.lproj``.

The views resolve interface strings through ``LS("日本語のリテラル")``
(``AppLanguage.text``), which looks the key up in
``Sources/MLXBar/Resources/en.lproj/Localizable.strings``. Japanese literals are
the source language, so a key that is absent from ``en.lproj`` does not fail
loudly -- it silently renders Japanese in the English interface (F-26/F-27,
AC-COMPAT-2).

This script extracts every key passed to ``LS(...)`` in ``Sources/MLXBar/**``
and compares it with the keys defined in ``en.lproj``. Both branches of a
ternary (``LS(flag ? "A" : "B")``) are collected; a plain ``LS("...")`` regex
misses those, which is how the four MTP/PLE keys stayed absent while the
interface kept building.

Stdlib only and deterministic: files are visited in sorted order, keys are
reported sorted, and nothing depends on the current working directory.

Usage (from anywhere):
    python3 scripts/check_i18n.py

Exit status: 0 when every referenced key exists, 1 when any is missing,
2 when the strings file or source tree cannot be found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "Sources" / "MLXBar"
STRINGS = SOURCE_DIR / "Resources" / "en.lproj" / "Localizable.strings"

#: ``LS(`` at a word boundary, so ``AppLanguage.text(`` is not matched.
LS_RE = re.compile(r"\bLS\s*\(")
#: One Swift string literal (the group is the raw body, escapes included).
LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
#: One ``"key" = "value";`` entry, anchored at the start of a line.
KEY_RE = re.compile(r'^\s*"((?:\\.|[^"\\])*)"\s*=', re.M)
#: An interpolated literal (``"\(model.name)"``) is not a static key.
INTERPOLATION = "\\("
#: Operators next to a literal mean the literal is data, not a key
#: (``LS(preset == "light" ? "A" : "B")``).
_COMPARISONS = ("==", "!=", "<=", ">=", "~=")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def strip_comments(source: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, preserving offsets and lines.

    Comment text can contain something that looks like ``LS("...")``; blanking
    it (rather than deleting it) keeps reported line numbers correct.
    """
    out: list[str] = []
    index, length = 0, len(source)
    in_string = False
    while index < length:
        char = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            while index < length and source[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and following == "*":
            out.append("  ")
            index += 2
            while index < length and not (source[index] == "*"
                                          and index + 1 < length
                                          and source[index + 1] == "/"):
                out.append("\n" if source[index] == "\n" else " ")
                index += 1
            if index < length:
                out.append("  ")
                index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def call_span(source: str, open_paren: int) -> str | None:
    """Text inside the parentheses that open at ``open_paren``.

    Strings are skipped so a ``)`` inside a literal cannot end the call early.
    Returns ``None`` when the call is unbalanced (a truncated file).
    """
    depth = 0
    index, length = open_paren, len(source)
    in_string = False
    while index < length:
        char = source[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren + 1:index]
        index += 1
    return None


def decode_literal(raw: str) -> str:
    """The Swift string a literal body denotes, for key comparison."""
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(raw):
            out.append("\\")
            break
        escape = raw[index]
        if escape in _ESCAPES:
            out.append(_ESCAPES[escape])
            index += 1
        elif escape == "u" and index + 5 <= len(raw):
            try:
                out.append(chr(int(raw[index + 1:index + 5], 16)))
                index += 5
            except ValueError:
                out.append("u")
                index += 1
        else:
            out.append(escape)
            index += 1
    return "".join(out)


def is_key_candidate(span: str, start: int, end: int) -> bool:
    """Whether the literal at ``span[start:end]`` can be an ``LS`` key.

    A ternary condition may contain its own literals -- ``preset == "light"``
    or a dictionary subscript ``api["requireToken"]`` -- and those are data, not
    interface strings. Keys sit alone in a branch, so anything glued to a
    comparison operator or bracket is skipped.
    """
    before = span[:start].rstrip()
    after = span[end:].lstrip()
    if before.endswith(_COMPARISONS) or after.startswith(_COMPARISONS):
        return False
    if before.endswith("[") or after.startswith("]") or after.startswith("["):
        return False
    return True


def localized_keys(path: Path) -> set[str]:
    """Every key defined in a ``.strings`` file."""
    text = strip_comments(path.read_text(encoding="utf-8"))
    return {decode_literal(raw) for raw in KEY_RE.findall(text)}


def referenced_keys(paths: list[Path]) -> tuple[dict[str, str], int]:
    """``LS()`` keys mapped to their first ``file:line`` reference."""
    found: dict[str, str] = {}
    for path in paths:
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for match in LS_RE.finditer(text):
            span = call_span(text, match.end() - 1)
            if span is None:
                continue
            line = text.count("\n", 0, match.start()) + 1
            location = f"{path.relative_to(ROOT)}:{line}"
            for literal in LITERAL_RE.finditer(span):
                raw = literal.group(1)
                if INTERPOLATION in raw:
                    continue
                if not is_key_candidate(span, literal.start(), literal.end()):
                    continue
                key = decode_literal(raw)
                if key and key not in found:
                    found[key] = location
    return found, len(found)


def main() -> int:
    if not STRINGS.is_file():
        print(f"check_i18n: strings file not found: {STRINGS}", file=sys.stderr)
        return 2
    if not SOURCE_DIR.is_dir():
        print(f"check_i18n: source tree not found: {SOURCE_DIR}", file=sys.stderr)
        return 2

    swift_files = sorted(SOURCE_DIR.rglob("*.swift"))
    known = localized_keys(STRINGS)
    referenced, total = referenced_keys(swift_files)
    missing = {key: where for key, where in referenced.items() if key not in known}

    target = STRINGS.relative_to(ROOT)
    if missing:
        print(f"check_i18n: {len(missing)} key(s) missing from {target}:")
        for key in sorted(missing):
            print(f"  {key!r}  ({missing[key]})")
        print(f"check_i18n: {total} key(s) referenced in {len(swift_files)} Swift file(s)")
        return 1

    print(f"check_i18n: OK — {total} key(s) referenced in {len(swift_files)} "
          f"Swift file(s), all present in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
