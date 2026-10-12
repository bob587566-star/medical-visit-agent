from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from backend.knowledge import Knowledge
from backend.retrieval import HybridRetriever


def source(identifier, title, content, topic="insomnia"):
    return {"id": identifier, "title": title, "content": content, "topics": [topic],
            "keywords": ["睡不着"] if topic == "insomnia" else ["咳嗽"],
            "url": "https://www.nhs.uk/conditions/insomnia/", "publisher": "NHS",
            "reviewed_at": "2026-10-12", "category": "健康资料"}


@pytest.fixture
def knowledge(tmp_path):
    path = tmp_path / "articles.json"
    path.write_text(json.dumps([
        source("lexical", "失眠：睡不着", "睡不着时记录入睡时间。"),
        source("semantic", "睡眠记录", "记录作息和日间影响。"),
        source("unrelated", "咳嗽记录", "记录咳嗽开始时间。", "cough"),
    ], ensure_ascii=False), encoding="utf-8")
    return Knowledge(path)


def local_mock(requests, *, error=None, query_error=False, vector_factory=None, digest="model-v1"):
    async def handle(request):
        assert request.url.scheme == "http"
        assert request.url.host == "127.0.0.1" and request.url.port == 11434
        requests.append(request)
        if error == "offline":
            raise httpx.ConnectError("local service unavailable", request=request)
        if error == "redirect":
            return httpx.Response(307, headers={"location": "https://example.invalid/patient"})
        if request.url.path == "/api/tags":
            models = [] if error == "missing_model" else [{"name": "bge-m3:latest", "digest": digest}]
            return httpx.Response(200, json={"models": models})
        assert request.url.path == "/api/embed"
        inputs = json.loads(request.content)["input"]
        if query_error and inputs == ["睡不着"]:
            raise httpx.ReadTimeout("local query timed out", request=request)
        if error == "malformed":
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * (len(inputs) + 1)})
        if error == "nan":
            return httpx.Response(200, text='{"embeddings":[[NaN,1]]}')
        if error == "zero":
            return httpx.Response(200, json={"embeddings": [[0.0, 0.0]] * len(inputs)})
        vectors = []
        for text in inputs:
            if vector_factory:
                vectors.append(vector_factory(text))
            else:
                vectors.append([0.0, 1.0] if text == "睡不着" or text.startswith("睡眠记录") else [1.0, 0.0])
        return httpx.Response(200, json={"embeddings": vectors})
    return httpx.MockTransport(handle)


def execute(coroutine):
    return asyncio.run(coroutine)


def test_curated_library_has_46_distinct_articles_and_safe_sources():
    library = Knowledge(Path(__file__).resolve().parents[2] / "knowledge" / "articles.json")
    articles = library.all()
    assert not library.load_error
    assert len(articles) == 46
    assert len({item.id for item in articles}) == 46
    assert {'crisis_support', 'poisoning', 'seizure', 'severe_bleeding', 'pregnancy_bleeding', 'infant_warning', 'head_injury', 'severe_haemoptysis'}.issubset({item.id for item in articles})
    assert len(library.categories()) == 6
    assert all(item.url.startswith("https://") for item in articles)
    assert {item.id for item in library.search("咳嗽两天")} == {"cough"}
    assert {item.id for item in library.search("孩子体温升高了")} == {"child_fever"}


@pytest.mark.parametrize("query", [
    "这家医院地址在哪？", "门诊在哪里？", "医保能报销多少？", "挂号费用是多少？",
    "头痛吃药吃多少毫克？", "给我开药", "我咳嗽但想问医院的地址", "XYZ987_UNKNOWN", "",
])
def test_unsupported_queries_never_rank_or_embed(knowledge, tmp_path, query):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            assert await retriever.search(query) == []
            assert requests == []
    execute(scenario())


def test_real_hybrid_algorithm_uses_local_embeddings_and_topic_guard(knowledge, tmp_path):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            assert knowledge.search("睡不着")[0].id == "lexical"
            results = await retriever.search("睡不着")
            assert results[0].id == "semantic"
            assert {item.id for item in results} == {"lexical", "semantic"}
            assert retriever.status() == {"mode": "hybrid_ollama", "embedding_available": True,
                                         "model": "bge-m3:latest", "document_count": 3}
            assert any(json.loads(request.content).get("input") == ["睡不着"]
                       for request in requests if request.method == "POST")
            cache = (tmp_path / "cache.json").read_text(encoding="utf-8")
            assert "睡不着" not in cache
    execute(scenario())


@pytest.mark.parametrize("error", ["offline", "missing_model", "redirect", "malformed", "nan", "zero"])
def test_local_failure_transparently_preserves_lexical_result(knowledge, tmp_path, error):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests, error=error), follow_redirects=True) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            assert [item.id for item in await retriever.search("睡不着")] == [item.id for item in knowledge.search("睡不着")]
            assert retriever.status()["mode"] == "lexical_fallback"
            assert not retriever.status()["embedding_available"]
            assert all(request.url.host == "127.0.0.1" for request in requests)
    execute(scenario())


def test_query_failure_switches_status_to_fallback(knowledge, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=local_mock([], query_error=True)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            await retriever.initialize()
            assert retriever.status()["embedding_available"]
            assert (await retriever.search("睡不着"))[0].id == "lexical"
            assert not retriever.status()["embedding_available"]
    execute(scenario())


def test_valid_cache_reuses_public_document_vectors_but_probes_availability(knowledge, tmp_path):
    async def scenario():
        cache = tmp_path / "cache.json"
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            await HybridRetriever(knowledge, client, cache).initialize()
            requests.clear()
            retriever = HybridRetriever(knowledge, client, cache)
            await retriever.initialize()
            post_bodies = [json.loads(request.content) for request in requests if request.method == "POST"]
            assert [body["input"] for body in post_bodies] == [["就诊准备"]]
            assert retriever.status()["embedding_available"]
    execute(scenario())


def test_content_change_even_same_mtime_and_size_invalidates_cache(knowledge, tmp_path):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            await retriever.initialize()
            before_fingerprint = retriever.fingerprint
            original_stat = knowledge.path.stat()
            old = knowledge.path.read_text(encoding="utf-8")
            knowledge.path.write_text(old.replace("记录作息", "记录睡眠"), encoding="utf-8")
            assert knowledge.path.stat().st_size == original_stat.st_size
            os.utime(knowledge.path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            assert not retriever.status()["embedding_available"]
            requests.clear()
            results = await retriever.search("睡不着")
            assert retriever.fingerprint != before_fingerprint
            assert any("记录睡眠" in item.content for item in results)
            assert any(len(json.loads(request.content)["input"]) == 3 for request in requests if request.method == "POST")
    execute(scenario())


def test_invalid_editorial_edit_fails_closed_and_no_stale_evidence(knowledge, tmp_path):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            await retriever.initialize()
            knowledge.path.write_text("not-json", encoding="utf-8")
            assert await retriever.search("睡不着") == []
            assert not retriever.status()["embedding_available"]
            assert retriever.status()["document_count"] == 0
    execute(scenario())


def test_concurrent_initialization_indexes_once(knowledge, tmp_path):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            await asyncio.gather(*[retriever.initialize() for _ in range(5)])
            assert sum(request.url.path == "/api/tags" for request in requests) == 1
            assert retriever.status()["embedding_available"]
    execute(scenario())


def test_zero_limit_is_empty_without_network(knowledge, tmp_path):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            assert await retriever.search("睡不着", 0) == []
            assert requests == []
    execute(scenario())


def test_query_dimension_change_does_not_use_unchecked_vectors(knowledge, tmp_path):
    async def scenario():
        factory = lambda text: [1.0, 0.0, 0.0] if text == "睡不着" else [1.0, 0.0]
        async with httpx.AsyncClient(transport=local_mock([], vector_factory=factory)) as client:
            retriever = HybridRetriever(knowledge, client, tmp_path / "cache.json")
            await retriever.initialize()
            assert retriever.status()["embedding_available"]
            assert (await retriever.search("睡不着"))[0].id == "lexical"
            assert not retriever.status()["embedding_available"]
    execute(scenario())


def test_changed_model_digest_invalidates_same_named_model_cache(knowledge, tmp_path):
    async def scenario():
        cache = tmp_path / "cache.json"
        async with httpx.AsyncClient(transport=local_mock([])) as first_client:
            await HybridRetriever(knowledge, first_client, cache).initialize()
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests, digest="model-v2")) as second_client:
            retriever = HybridRetriever(knowledge, second_client, cache)
            await retriever.initialize()
            assert retriever.model_digest == "model-v2"
            assert any(len(json.loads(request.content)["input"]) == 3 for request in requests if request.method == "POST")
    execute(scenario())


def test_corrupt_disk_cache_is_rebuilt_not_treated_as_evidence(knowledge, tmp_path):
    async def scenario():
        cache = tmp_path / "cache.json"
        cache.write_text('{"vectors": "corrupt"}', encoding="utf-8")
        requests = []
        async with httpx.AsyncClient(transport=local_mock(requests)) as client:
            retriever = HybridRetriever(knowledge, client, cache)
            await retriever.initialize()
            assert retriever.status()["embedding_available"]
            assert any(len(json.loads(request.content)["input"]) == 3 for request in requests if request.method == "POST")
            assert len(json.loads(cache.read_text(encoding="utf-8"))["vectors"]) == 3
    execute(scenario())
