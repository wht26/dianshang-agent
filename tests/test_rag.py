import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from wang.app import create_app
from wang.knowledge import Knowledge


HEADERS = {"X-Wang-Client": "web"}


def test_import_persists_deduplicates_and_isolates_products(tmp_path):
    app = create_app(tmp_path / "rag.db")
    with TestClient(app) as client:
        body = {"title": "T恤护理.txt", "content": "本款T恤的专属护理标记为青松护理，不可强力拧绞。", "product_id": "tee-001"}
        first = client.post("/api/knowledge/import", headers=HEADERS, json=body)
        assert first.status_code == 200
        doc_id = first.json()["id"]
        assert client.post("/api/knowledge/import", headers=HEADERS, json=body).json()["id"] == doc_id
        assert len(client.get("/api/knowledge").json()["documents"]) == 4
        restarted = Knowledge(db_path=tmp_path / "rag.db")
        assert restarted.search("青松护理", product_id="tee-001")[0]["id"] == doc_id
        assert not any(c["id"] == doc_id for c in restarted.search("青松护理", product_id="tote-001"))
        assert not any(c["id"] == doc_id for c in restarted.search("青松护理"))
        assert client.post(f"/api/knowledge/{doc_id}/remove", headers=HEADERS, json={}).status_code == 200
        assert not any(c["id"] == doc_id for c in app.state.agent.knowledge.search("青松护理", product_id="tee-001"))


def test_import_validation_and_scope(tmp_path):
    with TestClient(create_app(tmp_path / "rag.db")) as client:
        for body in [{"title": " ", "content": "hello"}, {"title": "test", "content": " "},
                     {"title": "test", "content": "x\x00"}, {"title": "test", "content": "x" * 100001},
                     {"title": "test", "content": "x", "product_id": "other"}]:
            assert client.post("/api/knowledge/import", headers=HEADERS, json=body).status_code == 422
        assert client.post("/api/knowledge/import", json={"title": "x", "content": "y"}).status_code == 403
        assert client.post("/api/knowledge/K001/remove", headers=HEADERS, json={}).status_code == 422


def test_imported_context_reaches_model_and_citation_is_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("WANG_MODE", "auto")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    app = create_app(tmp_path / "rag.db")
    kb = app.state.agent.knowledge
    doc_id = kb.import_document("T恤护理", "青松护理：轻柔清洗，不可强力拧绞。", "tee-001")
    app.state.agent.knowledge = Knowledge(db_path=tmp_path / "rag.db")
    real_client = httpx.AsyncClient

    def handler(request):
        messages = json.loads(request.content)["messages"]
        context = json.loads(messages[-2]["content"].split("\n", 1)[1])
        assert any(c["id"] == doc_id and "不可强力拧绞" in c["content"] for c in context["guidance"])
        return httpx.Response(200, json={"choices": [{"message": {"content": f"这款建议轻柔清洗，别强力拧绞。[{doc_id}]"}}]})

    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(app.state.agent.answer("青松护理怎么做？", [], "tee-001"))
    assert result["sources"] == [{"id": doc_id, "title": "T恤护理"}]


def test_overlapping_chunks_preserve_boundary(tmp_path):
    app = create_app(tmp_path / "rag.db")
    text = "甲" * 695 + "边界护理注意事项不可烘干" + "乙" * 800
    kb = app.state.agent.knowledge
    doc_id = kb.import_document("长资料", text)
    kb = Knowledge(db_path=tmp_path / "rag.db")
    assert any("边界护理注意事项不可烘干" in c["content"] for c in kb.search("边界护理注意事项"))
    assert len([c for c in kb.chunks if c["id"] == doc_id]) == 3


def test_customer_wording_matches_care_synonyms(tmp_path):
    kb = create_app(tmp_path / "rag.db").state.agent.knowledge
    doc_id = kb.import_document("护理补充", "应摊平阴干，避免悬挂拉扯肩部。", "tee-001")
    kb = Knowledge(db_path=tmp_path / "rag.db")
    assert any(c["id"] == doc_id for c in kb.search("怎么晾？肩膀变形", product_id="tee-001"))
