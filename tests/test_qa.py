import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from wang.agent import Agent, ProviderError
from wang.app import create_app
from wang.knowledge import Knowledge


HEADERS = {"X-Wang-Client": "web"}


def test_local_chinese_retrieval_and_custom_knowledge(tmp_path):
    (tmp_path / "notes.md").write_text("# 内部选品流程\n\n测试独有商品蓝鲸杯，需要先完成样品质检。", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("不读取此文件", encoding="utf-8")
    knowledge = Knowledge(tmp_path)
    assert len(knowledge.documents) == 4
    rows = knowledge.search("蓝鲸杯样品质检")
    assert rows[0]["title"] == "内部选品流程"
    assert "样品质检" in rows[0]["content"]
    assert knowledge.search("unmatched_xyz") == []
    assert any("尺码" in row["content"] for row in knowledge.search("服装尺码"))
    with pytest.raises(ValueError):
        Knowledge(tmp_path / "missing")


def test_no_key_local_excerpts_are_labeled(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agent = Agent(Knowledge())
    result = asyncio.run(agent.answer("T恤洗护", []))
    assert result["mode"] == "local" and result["sources"]
    assert "仅为本地资料检索摘录" in result["answer"]
    empty = asyncio.run(agent.answer("unmatched_xyz", []))
    assert "咨询具体款式时请说明商品" in empty["answer"] and empty["product_id"] is None


def test_provider_memory_sources_and_no_business_tools(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("WANG_MODE", "auto")
    real_client = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        assert "tools" not in body
        assert body["messages"][-1]["content"] == "那应该怎么洗？"
        assert any(m["content"] == "我在看T恤" for m in body["messages"])
        context = json.loads(body["messages"][-2]["content"].split("\n", 1)[1])
        assert context["store"]["product_id"] == "tee-001"
        assert len(context["store"]["product_facts"]) == 1
        source = context["store"]["product_facts"][0]["id"]
        return httpx.Response(200, json={"choices": [{"message": {"content": f"按相同口径计算[{source}，本地学习资料]。假引用[K999]"}}],
                                         "usage": {"total_tokens": 30}})

    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(Agent(Knowledge()).answer("那应该怎么洗？", [
        {"question": "我在看T恤", "answer": "您好", "mode": "deepseek", "product_id": "tee-001"}]))
    assert result["sources"] and "K999" not in result["answer"]
    assert "来源未核验" in result["answer"] and result["usage"]["total_tokens"] == 30


@pytest.mark.parametrize("status,body", [
    (401, {"error": "secret upstream response"}),
    (200, {"choices": []}),
    (200, {"choices": [{"message": {"content": ""}}]}),
    (200, {"choices": [{"message": {"content": None}}]}),
])
def test_provider_failures_never_expose_response(monkeypatch, status, body):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("WANG_MODE", "auto")
    real_client = httpx.AsyncClient
    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)), **kw))
    with pytest.raises(ProviderError) as error:
        asyncio.run(Agent(Knowledge()).answer("你好", []))
    assert "secret" not in str(error.value) and "test-only" not in str(error.value)


def test_http_no_login_memory_isolation_and_new_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("WANG_MODE", "demo")
    app = create_app(tmp_path / "qa.db")
    with TestClient(app) as first, TestClient(app) as second:
        boot = first.get("/api/bootstrap")
        assert boot.status_code == 200 and boot.json()["history"] == []
        assert boot.json()["live_web"] is False
        assert "httponly" in boot.headers["set-cookie"].lower()
        assert "samesite=strict" in boot.headers["set-cookie"].lower()
        token = first.cookies.get("wang_support")
        response = first.post("/api/chat", headers=HEADERS, json={"message": "T恤面料"})
        assert response.status_code == 200 and response.json()["sources"]
        assert len(first.get("/api/bootstrap").json()["history"]) == 1
        assert second.get("/api/bootstrap").json()["history"] == []
        assert first.post("/api/conversations/new", headers=HEADERS, json={}).status_code == 200
        assert first.cookies.get("wang_support") != token
        assert first.get("/api/bootstrap").json()["history"] == []
        with app.state.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM qa_turns").fetchone()["count"] == 1
            assert token not in str(db.execute("SELECT * FROM qa_sessions").fetchall())
        for path in ["/api/orders", "/api/returns", "/api/auth/status", "/api/staff/dashboard"]:
            assert first.get(path).status_code == 404
        assert first.post("/api/returns/confirm", headers=HEADERS, json={}).status_code == 404
        assert "服装鞋包" in first.get("/").text


def test_network_timeout_is_sanitized(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("WANG_MODE", "auto")
    real_client = httpx.AsyncClient

    def timeout(request):
        raise httpx.ReadTimeout("private endpoint and request details", request=request)

    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(timeout), **kw))
    with pytest.raises(ProviderError) as error:
        asyncio.run(Agent(Knowledge()).answer("ROAS", []))
    assert "private" not in str(error.value) and "test-only" not in str(error.value)


def test_knowledge_documents_are_packaged_and_oversize_files_skipped(tmp_path):
    (tmp_path / "oversize.md").write_text("x" * 100_001, encoding="utf-8")
    knowledge = Knowledge(tmp_path)
    assert len(knowledge.documents) == 3
    assert all(document["file"] != "oversize.md" for document in knowledge.documents)


def test_http_boundaries_validation_and_failure_does_not_save(tmp_path, monkeypatch):
    app = create_app(tmp_path / "qa.db")

    async def fail(*args):
        raise ProviderError("模型暂时不可用。")

    monkeypatch.setattr(app.state.agent, "answer", fail)
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"message": "hello"}).status_code == 403
        assert client.post("/api/chat", headers=HEADERS | {"Origin": "https://other.invalid"},
                           json={"message": "hello"}).status_code == 403
        for message in [" ", "x" * 6001]:
            assert client.post("/api/chat", headers=HEADERS, json={"message": message}).status_code == 422
        assert client.post("/api/chat", headers=HEADERS, json={"message": "hello"}).status_code == 502
        assert client.get("/api/bootstrap").json()["history"] == []
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_concurrent_generation_is_bounded(tmp_path):
    async def scenario():
        app = create_app(tmp_path / "qa.db")
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(*args):
            entered.set()
            await release.wait()
            return {"answer": "回答", "sources": [], "mode": "local", "usage": {}}

        app.state.agent.answer = answer
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.get("/api/bootstrap")
            first = asyncio.create_task(client.post("/api/chat", headers=HEADERS, json={"message": "问题"}))
            await entered.wait()
            assert (await client.post("/api/chat", headers=HEADERS, json={"message": "重复"})).status_code == 429
            assert (await client.post("/api/conversations/new", headers=HEADERS, json={})).status_code == 429
            release.set()
            assert (await first).status_code == 200
    asyncio.run(scenario())
