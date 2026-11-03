from fastapi.testclient import TestClient

from wang.app import create_app
from wang.knowledge import Knowledge
from wang.shop import Shop


def test_default_shop_has_no_fabricated_products_or_policy(monkeypatch):
    monkeypatch.delenv("WANG_SHOP_FILE", raising=False)
    shop = Shop()
    assert not shop.config.is_demo and shop.catalog() == []
    assert shop.context("什么时候发货", [])["policy"]["content"] == {}


def test_real_shop_import_persistence_and_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("WANG_SHOP_FILE", raising=False)
    path = tmp_path / "shop.db"
    headers = {"X-Wang-Client": "web"}
    data = {"name": "我的服装店", "is_demo": False, "policies": {"配送": "以订单页面约定为准"},
            "products": [{"id": "shirt-real", "name": "门店衬衫", "specs": {"面料": "70%棉30%涤纶"}}]}
    with TestClient(create_app(path)) as client:
        assert client.get("/api/bootstrap").json()["products"] == []
        assert client.post("/api/shop/import", headers=headers, json=data).status_code == 200
        assert client.get("/api/bootstrap").json()["shop"]["name"] == "我的服装店"
        assert client.post("/api/shop/import", headers=headers, json=data | {
            "products": [data["products"][0], data["products"][0]]}).status_code == 422
    with TestClient(create_app(path)) as client:
        assert client.get("/api/bootstrap").json()["products"][0]["id"] == "shirt-real"


def test_apparel_topics_retrievable_without_fake_sku():
    knowledge = Knowledge()
    for question, expected in [
        ("羽绒服和棉服的填充", "充绒量"),
        ("牛仔裤怎么选腰围臀围", "牛仔裤"),
        ("衬衫雪纺是真丝吗", "雪纺"),
        ("文胸内衣怎么选罩杯", "罩杯"),
        ("童装年龄和尺码", "童装"),
        ("羊绒毛衣会不会扎", "羊绒"),
    ]:
        assert any(expected in row["content"] for row in knowledge.search(question)), question
