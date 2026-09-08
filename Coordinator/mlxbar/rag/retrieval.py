"""Pure-Python vector ranking and context assembly.

No numpy: the coordinator ships without it and the working set is bounded by
``rag.maxChunksPerCollection``, so a plain Python cosine over a few thousand
vectors is well within budget for an interactive query. If that ceiling is
ever raised far enough to matter, this is the one place to swap in a native
dot product -- the interface would not change.
"""

from __future__ import annotations

import math

CONTEXT_HEADER = {
    "ja": "以下はナレッジベースから取得した参考情報です。関連する場合のみ利用し、"
          "情報が不足する場合はその旨を伝えてください。",
    "en": "The following passages were retrieved from a knowledge base. Use them "
          "only where relevant, and say so if they do not contain the answer.",
}


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector)) or 1.0


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (_norm(a) * _norm(b))


def rank(query_vector: list[float], chunks: list[dict], top_k: int) -> list[dict]:
    """Return the ``top_k`` chunks most similar to ``query_vector``.

    ``chunks`` are dicts from ``RagStore.load_chunks``. Each result is a copy
    with a ``score`` added, ordered by descending score.
    """
    top_k = max(1, int(top_k))
    scored: list[dict] = []
    for chunk in chunks:
        vector = chunk.get("embedding") or []
        score = cosine(query_vector, vector)
        scored.append({**{key: value for key, value in chunk.items() if key != "embedding"},
                       "score": round(score, 6)})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def build_context_block(chunks: list[dict], max_chars: int, language: str = "en") -> str:
    """One synthetic system message carrying the retrieved passages.

    Passages are added in rank order until ``max_chars`` would be exceeded; at
    least the top passage is always included (truncated if it alone is over
    budget) so a request that asked for context never silently gets none.
    """
    header = CONTEXT_HEADER.get(language, CONTEXT_HEADER["en"])
    max_chars = max(200, int(max_chars))
    lines: list[str] = [header, ""]
    budget = max_chars - len(header) - 1
    used = 0
    for index, chunk in enumerate(chunks, start=1):
        title = (chunk.get("documentTitle") or "").strip()
        label = f"[{index}]" + (f" {title}" if title else "")
        body = (chunk.get("text") or "").strip()
        entry = f"{label}\n{body}"
        if index > 1 and used + len(entry) + 2 > budget:
            break
        if index == 1 and len(entry) > budget:
            entry = entry[:max(0, budget)]
        lines.append(entry)
        lines.append("")
        used += len(entry) + 2
    return "\n".join(lines).strip()


def inject_context_block(messages: list[dict], block: str) -> list[dict]:
    """Prepend ``block`` as its own leading ``system`` message.

    Kept as a separate message (rather than folded into an existing system
    prompt) so it is easy to see in a log and cannot disturb a client's own
    system instruction. Mirrors ``response_format.inject_into_messages`` in
    placing itself at index 0, which is also the slice
    ``context_compression`` always keeps verbatim.
    """
    if not block:
        return messages
    return [{"role": "system", "content": block}, *messages]
