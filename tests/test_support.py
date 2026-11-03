import json

import pytest
from fastapi.testclient import TestClient

from wang.app import create_app
from wang.shop import Shop


def test_selected_product_and_explicit_switch_do_not_mix_parameters():
    shop = Shop()
    context = shop.context("是什么面料？", [], "tee-001")
    assert context["product_id"] == "tee-001"
    assert len(context["product_facts"]) == 1
    assert context["product_facts"][0]["content"]["specs"]["面料"] == "100%棉（演示资料）"
    context = shop.context("随行帆布托特包是什么材质？", [], "tee-001")
    assert context["product_id"] == "tote-001"
    assert "不是真皮" in context["product_facts"][0]["content"]["specs"]["材质"]
    assert "面料" not in context["product_facts"][0]["content"]["specs"]


def test_missing_product_followup_and_comparison():
    shop = Shop()
    assert shop.context("什么颜色？", [])["product_facts"] == []
    history = [{"product_id": "sneaker-001"}]
    assert shop.context("那怎么洗？", history)["product_id"] == "sneaker-001"
    assert shop.context("什么颜色？", history, "")["product_facts"] == []
    context = shop.context("T恤和帆布包怎么洗？", [])
    assert len(context["product_facts"]) == 2 and context["product_id"] is None
    with pytest.raises(ValueError):
        shop.context("颜色", [], "nonexistent")


def test_custom_shop_replaces_demo_and_missing_policy_stays_missing(tmp_path):
    path = tmp_path / "shop.json"
    path.write_text(json.dumps({"name": "测试店铺", "is_demo": False,
                               "products": [{"id": "shirt-x", "name": "测试衬衫", "specs": {"颜色": "红色"}}]}),
                    encoding="utf-8")
    shop = Shop(path)
    context = shop.context("测试衬衫多少钱？", [])
    assert context["policy"]["content"] == {}
    assert context["is_demo"] is False
    assert context["product_facts"][0]["content"]["price"] == "未提供"
    serialized = json.dumps(context, ensure_ascii=False)
    assert "青禾" not in serialized and "7天" not in serialized and "T恤" not in serialized


@pytest.mark.parametrize("change", [
    {"products": [{"id": "x", "name": "a"}, {"id": "x", "name": "b"}]},
    {"products": [{"id": "x", "name": "a", "price_cents": -1}]},
    {"products": [{"id": "x", "name": "a", "price_cents": "100"}]},
    {"unknown_field": "bad"},
])
def test_invalid_shop_configuration_fails_early(tmp_path, change):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "test", "products": [{"id": "x", "name": "a"}], **change}), encoding="utf-8")
    with pytest.raises(ValueError):
        Shop(path)


def test_http_product_context_persists_and_unknown_product_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WANG_MODE", "demo")
    app = create_app(tmp_path / "support.db")
    headers = {"X-Wang-Client": "web"}
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["shop"]["is_demo"] is True and len(bootstrap["products"]) == 3
        response = client.post("/api/chat", headers=headers, json={"message": "这款材质是什么？", "product_id": "tee-001"})
        assert response.status_code == 200 and response.json()["product_id"] == "tee-001"
        history = client.get("/api/bootstrap").json()["history"]
        assert history[-1]["product_id"] == "tee-001"
        response = client.post("/api/chat", headers=headers, json={"message": "那怎么洗？"})
        assert response.json()["product_id"] == "tee-001"
        assert client.post("/api/chat", headers=headers,
                           json={"message": "颜色？", "product_id": "missing"}).status_code == 422
