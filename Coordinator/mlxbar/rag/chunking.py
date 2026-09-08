"""Recursive character text splitting.

Pure Python, no dependencies. The splitter walks a fixed list of separators
from coarse (blank lines) to fine (spaces), keeping every produced chunk at or
below ``chunk_size`` characters where the text allows it, and then re-joins
adjacent fragments up to that budget so a chunk is not needlessly small. A
trailing ``chunk_overlap`` characters of each chunk are prepended to the next
so a fact that straddles a boundary is still retrievable from one chunk.

Character counts, not tokens: the coordinator has no tokenizer of its own (the
tokenizers live in the worker venvs) and a character budget is a safe, stable
proxy that never drifts with a runtime update.
"""

from __future__ import annotations

# Coarse to fine. The empty string is the final fallback: a run of text with no
# separator at all (a long base64 blob, CJK without spaces) is still cut at the
# hard character bound rather than returned oversized.
_SEPARATORS = ["\n\n", "\n", "。", "．", ". ", "！", "？", "!", "?", "；", ";", "、", ",", " ", ""]


def _split_with(text: str, separator: str) -> list[str]:
    if separator == "":
        return list(text)
    parts = text.split(separator)
    # Keep the separator attached to the fragment it followed so re-joining is
    # lossless for everything except the exact split point.
    return [part + separator for part in parts[:-1]] + parts[-1:]


def _recurse(text: str, chunk_size: int, separators: list[str]) -> list[str]:
    if len(text) <= chunk_size:
        return [text] if text else []
    separator = separators[0] if separators else ""
    rest = separators[1:] if len(separators) > 1 else [""]
    pieces: list[str] = []
    for fragment in _split_with(text, separator):
        if not fragment:
            continue
        if len(fragment) <= chunk_size:
            pieces.append(fragment)
        else:
            pieces.extend(_recurse(fragment, chunk_size, rest))
    return pieces


def split_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    """Split ``text`` into overlapping chunks of at most ~``chunk_size`` chars.

    Returns an empty list for blank input. ``chunk_overlap`` is clamped to
    below ``chunk_size`` so progress is always made.
    """
    text = (text or "").strip()
    if not text:
        return []
    chunk_size = max(1, int(chunk_size))
    chunk_overlap = max(0, min(int(chunk_overlap), chunk_size - 1))
    if len(text) <= chunk_size:
        return [text]

    fragments = _recurse(text, chunk_size, _SEPARATORS)

    # Merge adjacent fragments up to the size budget, then carry a tail of the
    # previous chunk into the next as the overlap.
    chunks: list[str] = []
    current = ""
    for fragment in fragments:
        if current and len(current) + len(fragment) > chunk_size:
            chunks.append(current.strip())
            if chunk_overlap and len(current) > chunk_overlap:
                current = current[-chunk_overlap:] + fragment
            else:
                current = fragment
        else:
            current += fragment
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]
