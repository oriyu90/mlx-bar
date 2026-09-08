"""Client for an external OpenAI-compatible ``/v1/embeddings`` endpoint.

MLXBar does not compute embeddings itself: the coordinator stays on its four
light dependencies and adds no worker type. Instead the user points this at any
OpenAI-compatible embeddings server -- LM Studio (``http://127.0.0.1:1234/v1``),
Ollama, llama.cpp, a cloud provider -- and MLXBar orchestrates chunking,
storage and retrieval around it.

Every call is timeout-bounded and every failure is turned into a typed
``MLXBarError`` (``RAG_EMBEDDING_UNAVAILABLE`` / ``RAG_EMBEDDING_DIM_MISMATCH``);
nothing here raises a bare ``httpx`` exception at a caller, and
``asyncio.CancelledError`` is never swallowed.
"""

from __future__ import annotations

import asyncio

import httpx

from ..errors import MLXBarError


def _clean_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise MLXBarError("RAG_EMBEDDING_NOT_CONFIGURED",
                          "埋め込みエンドポイントのURLが設定されていません", 400, False)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise MLXBarError("RAG_EMBEDDING_NOT_CONFIGURED",
                          "埋め込みエンドポイントのURLは http:// または https:// で指定してください",
                          400, False)
    # Accept a bare host, ".../v1" or the full ".../v1/embeddings".
    if url.endswith("/embeddings"):
        return url
    if url.endswith("/v1"):
        return url + "/embeddings"
    return url + "/v1/embeddings"


class EmbeddingClient:
    def __init__(self, base_url: str, model: str, *, token: str | None = None,
                 timeout_seconds: float = 30.0, batch_size: int = 32):
        self.endpoint = _clean_base_url(base_url)
        self.model = (model or "").strip()
        self.token = token or None
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.batch_size = max(1, int(batch_size))
        if not self.model:
            raise MLXBarError("RAG_EMBEDDING_NOT_CONFIGURED",
                              "埋め込みモデル名が設定されていません", 400, False)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _post_batch(self, client: httpx.AsyncClient, inputs: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": inputs}
        last_error: Exception | None = None
        for attempt in range(2):  # one retry; a cold embedding server is common
            try:
                response = await client.post(self.endpoint, json=payload, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise MLXBarError(
                    "RAG_EMBEDDING_UNAVAILABLE",
                    f"埋め込みエンドポイント（{self.endpoint}）に接続できません: {exc}",
                    503, True,
                ) from exc
            if response.status_code >= 400:
                detail = response.text[:200]
                raise MLXBarError(
                    "RAG_EMBEDDING_UNAVAILABLE",
                    f"埋め込みエンドポイントがエラーを返しました（HTTP {response.status_code}）: {detail}",
                    503, True,
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                                  "埋め込みエンドポイントの応答が不正です", 503, True) from exc
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(inputs):
                raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                                  "埋め込みエンドポイントの応答件数が一致しません", 502, True)
            vectors: list[list[float]] = []
            for item in items:
                vector = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(vector, list) or not vector:
                    raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                                      "埋め込みベクトルを取得できませんでした", 502, True)
                try:
                    vectors.append([float(value) for value in vector])
                except (TypeError, ValueError) as exc:
                    raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                                      "埋め込みベクトルの形式が不正です", 502, True) from exc
            return vectors
        raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                          f"埋め込みエンドポイントに接続できません: {last_error}", 503, True)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in order, in batches. Raises ``MLXBarError`` on any failure."""
        clean = [text if isinstance(text, str) else str(text) for text in texts]
        if not clean:
            return []
        results: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for start in range(0, len(clean), self.batch_size):
                batch = clean[start:start + self.batch_size]
                results.extend(await self._post_batch(client, batch))
        dims = {len(vector) for vector in results}
        if len(dims) > 1:
            raise MLXBarError("RAG_EMBEDDING_DIM_MISMATCH",
                              "埋め込みベクトルの次元が揃っていません", 502, True)
        return results

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        if not vectors:
            raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                              "クエリの埋め込みを取得できませんでした", 503, True)
        return vectors[0]

    async def probe(self) -> dict:
        """Reachability check for the settings UI. Never raises."""
        try:
            vector = await self.embed_one("ping")
        except MLXBarError as exc:
            return {"reachable": False, "code": exc.code, "message": exc.message,
                    "endpoint": self.endpoint, "model": self.model}
        return {"reachable": True, "dim": len(vector),
                "endpoint": self.endpoint, "model": self.model}
