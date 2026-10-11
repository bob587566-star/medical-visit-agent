"""Local-only semantic/lexical ranking of supported editorial health topics.

Embedding scores determine document order, never a diagnosis or medical
confidence. No query is cached and the only HTTP destination is local Ollama.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from pathlib import Path

import httpx

from .knowledge import Knowledge
from .schemas import Source

OLLAMA_URL = "http://127.0.0.1:11434"
CACHE_VERSION = 1


def _unit_vector(value: object, dimension: int | None = None) -> list[float]:
    if not isinstance(value, list) or not 2 <= len(value) <= 16384:
        raise ValueError("invalid embedding shape")
    if dimension is not None and len(value) != dimension:
        raise ValueError("embedding dimension changed")
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) for number in value):
        raise ValueError("invalid embedding values")
    norm = math.sqrt(sum(float(number) ** 2 for number in value))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("empty embedding")
    return [float(number) / norm for number in value]


class HybridRetriever:
    def __init__(self, knowledge: Knowledge, client: httpx.AsyncClient, cache_path: Path,
                 model: str = "bge-m3:latest"):
        self.knowledge = knowledge
        self.client = client
        self.cache_path = Path(cache_path)
        if not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", model):
            raise ValueError("invalid local embedding model name")
        self.model = model
        self.vectors: dict[str, list[float]] = {}
        self.fingerprint = ""
        self.model_digest = ""
        self.embedding_available = False
        self._lock = asyncio.Lock()
        self._retry_after = 0.0

    def status(self) -> dict:
        items = self.knowledge.all()
        # Knowledge edits invalidate the status even before the next search.
        available = self.embedding_available and self.fingerprint == self._fingerprint(items)
        return {"mode": "hybrid_ollama" if available else "lexical_fallback",
                "embedding_available": available, "model": self.model,
                "document_count": len(items)}

    @staticmethod
    def _fingerprint(items: list[Source]) -> str:
        payload = [item.model_dump(mode="json") for item in items]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    async def _embed(self, texts: list[str], timeout: float = 6.0) -> list[list[float]]:
        response = await self.client.post(
            OLLAMA_URL + "/api/embed",
            json={"model": self.model, "input": texts, "truncate": False, "keep_alive": "5m"},
            timeout=timeout, follow_redirects=False,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ValueError("embedding response count mismatch")
        dimension = len(embeddings[0]) if embeddings and isinstance(embeddings[0], list) else None
        return [_unit_vector(value, dimension) for value in embeddings]

    def _load_cache(self, fingerprint: str, digest: str, ids: set[str]) -> dict[str, list[float]] | None:
        try:
            if self.cache_path.stat().st_size > 12_000_000:
                return None
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if (cache.get("version") != CACHE_VERSION or cache.get("model") != self.model
                    or cache.get("fingerprint") != fingerprint or cache.get("model_digest") != digest):
                return None
            vectors = cache.get("vectors")
            if not isinstance(vectors, dict) or set(vectors) != ids:
                return None
            dimension = None
            checked = {}
            for key, value in vectors.items():
                checked[key] = _unit_vector(value, dimension)
                dimension = len(checked[key])
            return checked
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save_cache(self, vectors: dict[str, list[float]], fingerprint: str, digest: str) -> None:
        payload = {"version": CACHE_VERSION, "model": self.model, "model_digest": digest,
                   "fingerprint": fingerprint, "vectors": vectors}
        # Cache contains only public source vectors/IDs, never patient questions.
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(self.cache_path)
        except OSError:
            # Retrieval may use validated in-memory vectors without a disk cache.
            pass

    async def initialize(self) -> None:
        async with self._lock:
            items = self.knowledge.all()
            fingerprint = self._fingerprint(items)
            if self.embedding_available and self.fingerprint == fingerprint:
                return
            if fingerprint == self.fingerprint and time.monotonic() < self._retry_after:
                return
            self.embedding_available = False
            self.vectors = {}
            self.fingerprint = fingerprint
            if not items:
                self._retry_after = time.monotonic() + 15
                return
            try:
                response = await self.client.get(OLLAMA_URL + "/api/tags", timeout=2.0, follow_redirects=False)
                response.raise_for_status()
                installed = response.json().get("models", [])
                entry = next((entry for entry in installed if isinstance(entry, dict)
                              and entry.get("name", entry.get("model")) == self.model), None)
                if entry is None:
                    raise ValueError("local embedding model not installed")
                digest = str(entry.get("digest", ""))
                # Probe actual embedding availability before trusting a saved cache.
                probe = (await self._embed(["就诊准备"], timeout=15.0))[0]
                vectors = self._load_cache(fingerprint, digest, {item.id for item in items})
                if vectors and any(len(value) != len(probe) for value in vectors.values()):
                    vectors = None
                if vectors is None:
                    vectors = {}
                    for offset in range(0, len(items), 8):
                        batch = items[offset:offset + 8]
                        texts = [item.title + "\n" + "、".join(item.keywords) + "\n" + item.content for item in batch]
                        values = await self._embed(texts, timeout=30.0)
                        if any(len(value) != len(probe) for value in values):
                            raise ValueError("embedding dimensions disagree")
                        vectors.update({item.id: value for item, value in zip(batch, values)})
                    self._save_cache(vectors, fingerprint, digest)
                # A file may change while the asynchronous local indexing runs.
                if fingerprint != self._fingerprint(self.knowledge.all()):
                    raise ValueError("knowledge changed during indexing")
                self.vectors = vectors
                self.model_digest = digest
                self.embedding_available = True
                self._retry_after = 0.0
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
                self.vectors = {}
                self.embedding_available = False
                self._retry_after = time.monotonic() + 15

    async def search(self, query: str, limit: int = 3) -> list[Source]:
        if limit <= 0:
            return []
        candidates = self.knowledge.candidates(query)
        if not candidates:
            return []
        lexical = self.knowledge.search(query, limit=len(candidates))
        await self.initialize()
        # Re-read after awaits so a freshly indexed edit never returns an old Source.
        candidates = self.knowledge.candidates(query)
        lexical = self.knowledge.search(query, limit=len(candidates))
        if not candidates:
            return []
        if not self.embedding_available:
            return lexical[:limit]
        try:
            dimension = len(next(iter(self.vectors.values())))
            query_vector = _unit_vector((await self._embed([query]))[0], dimension)
            eligible_ids = {item.id for item in candidates}
            # Neither semantic similarity nor fusion can introduce unsupported topics.
            semantic = sorted(
                ((sum(a * b for a, b in zip(query_vector, self.vectors[item.id])), item)
                 for item in candidates if item.id in self.vectors),
                key=lambda pair: (-pair[0], pair[1].id),
            )
            score = {item.id: 0.35 / (60 + rank) for rank, item in enumerate(lexical, 1)}
            for rank, (_, item) in enumerate(semantic, 1):
                score[item.id] = score.get(item.id, 0) + 0.65 / (60 + rank)
            # Fail closed if an edit raced with embedding instead of mixing old vectors.
            if self.fingerprint != self._fingerprint(self.knowledge.all()):
                self.embedding_available = False
                return self.knowledge.search(query, limit)
            return sorted((item for item in candidates if item.id in eligible_ids),
                          key=lambda item: (-score.get(item.id, 0), item.id))[:limit]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, StopIteration, OverflowError):
            self.embedding_available = False
            self._retry_after = time.monotonic() + 15
            return self.knowledge.search(query, limit)
