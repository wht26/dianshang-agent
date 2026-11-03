import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from wang.app import create_app
from wang.memory import Memory, Preferences, short_memory
from wang.planner import make_plan
from wang.shop import Shop


HEADERS = {"X-Wang-Client": "web"}


def test_preferences_survive_new_chat_restart_and_are_isolated(tmp_path):
    path = tmp_path / "memory.db"
    app = create_app(path)
    with TestClient(app) as first, TestClient(app) as second:
        first.get("/api/bootstrap")
        owner_cookie = first.cookies.get("wang_memory")
        assert owner_cookie and owner_cookie != first.cookies.get("wang_support")
        assert first.post("/api/memory", headers=HEADERS, json={"fit": "喜欢宽松"}).status_code == 200
        first.post("/api/conversations/new", headers=HEADERS, json={})
        assert first.get("/api/memory").json()["preferences"]["fit"] == "喜欢宽松"
        assert second.get("/api/memory").json()["preferences"]["fit"] == ""
        assert Memory(app.state.store).read(owner_cookie)["fit"] == "喜欢宽松"
        with app.state.store.db() as db:
            assert owner_cookie not in str([dict(row) for row in db.execute("SELECT * FROM customer_memory")])
        first.post("/api/memory/clear", headers=HEADERS, json={})
        assert first.get("/api/memory").json()["preferences"] == Preferences().model_dump()


def test_external_memory_roundtrip_and_validation(tmp_path):
    with TestClient(create_app(tmp_path / "memory.db")) as client:
        original = {"fit": "偏宽松", "colors": "深蓝", "materials": "棉", "reply_style": "先说结论"}
        client.post("/api/memory", headers=HEADERS, json=original)
        exported = client.get("/api/memory").json()
        assert set(exported) == {"schema_version", "preferences"}
        client.post("/api/memory/clear", headers=HEADERS, json={})
        assert client.post("/api/memory/import", headers=HEADERS, json=exported).json()["preferences"] == original
        for body in [exported | {"schema_version": 2}, exported | {"extra": "bad"},
                     {"schema_version": 1, "preferences": {"fit": "x" * 101}},
                     {"schema_version": 1, "preferences": {"phone": "13900000000"}}]:
            assert client.post("/api/memory/import", headers=HEADERS, json=body).status_code == 422
        assert client.post("/api/memory", headers=HEADERS, json={"fit": "13900000000"}).status_code == 422
        assert client.get("/api/memory").json()["preferences"] == original
        assert client.post("/api/memory", json={}).status_code == 403


def test_short_memory_budget_and_user_only_older_notes():
    history = [{"question": f"用户{i}" + "甲" * 3000, "answer": "乙" * 8000, "mode": "deepseek"} for i in range(20)]
    result = short_memory(history)
    assert sum(len(t["question"]) + len(t["answer"]) for t in result["recent"]) <= 16000
    assert len(result["recent"]) <= 8 and len(result["earlier_user_statements"]) <= 4
    assert all("乙" not in note and len(note) <= 160 for note in result["earlier_user_statements"])
    assert result["recent"][-1]["question"].startswith("用户19")


def test_planner_quality_missing_fields_and_existing_answers():
    context = Shop().context("T恤质量问题", [])
    plan = make_plan("T恤洗过后缝线开裂，想退货", [], context, {})
    assert "quality" in plan["intents"] and "return" in plan["intents"]
    assert "大致收货时间" in plan["missing_information"]
    assert "使用或洗涤情况" not in plan["missing_information"]
    plan = make_plan("能退货吗？", [{"question": "收到3天了，没洗", "product_id": "tee-001"}], context, {})
    assert "大致收货时间" not in plan["missing_information"]
    assert "使用或洗涤情况" not in plan["missing_information"]
    plan = make_plan("直接退款", [], context, {})
    assert plan["needs_external_verification"]


def test_model_receives_memory_and_plan_without_turning_it_into_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("WANG_MODE", "auto")
    agent = create_app(tmp_path / "m.db").state.agent
    real_client = httpx.AsyncClient

    def handler(request):
        data = json.loads(request.content)
        context = json.loads(data["messages"][-2]["content"].split("\n", 1)[1])
        assert context["customer_preferences"]["fit"] == "宽松"
        assert "sizing" in context["support_plan"]["intents"]
        assert "本轮明确要求" in data["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "请对照这款尺码表，不只凭身高体重判断。[P001]"}}]})

    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(agent.answer("T恤选什么尺码？", [], "tee-001", {"fit": "宽松"}))
    assert result["plan"]["planner_type"] == "rules-v1"
    assert "尺码" in result["answer"]


@pytest.mark.parametrize("question", ["请记住我喜欢宽松", "我的手机号是13900000000"])
def test_chat_does_not_automatically_save_long_term_memory(tmp_path, monkeypatch, question):
    monkeypatch.setenv("WANG_MODE", "demo")
    with TestClient(create_app(tmp_path / "m.db")) as client:
        client.get("/api/bootstrap")
        assert client.post("/api/chat", headers=HEADERS, json={"message": question}).status_code == 200
        assert client.get("/api/memory").json()["preferences"] == Preferences().model_dump()
