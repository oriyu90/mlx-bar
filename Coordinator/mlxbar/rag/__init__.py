"""Local knowledge base (RAG) for MLXBar.

A small, dependency-free retrieval layer that lives entirely inside the
coordinator: it splits documents into overlapping character chunks
(``chunking``), turns them into vectors via an *external* OpenAI-compatible
``/v1/embeddings`` endpoint (``embeddings``), stores them in a SQLite file of
its own (``store``), and ranks them against a query with pure-Python cosine
similarity (``retrieval``). ``service.RagService`` ties those together and is
the only object the rest of the app touches.

Everything here is dormant unless ``rag.enabled`` is set: an ordinary API
request never reaches this package, and ``rag.sqlite3`` is not created until
the first collection is made. See ``DESIGN_v2.1.0.md``.
"""

from __future__ import annotations

from .service import RagService

__all__ = ["RagService"]
