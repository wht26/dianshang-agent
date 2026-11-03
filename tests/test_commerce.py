"""Money/stock boundaries and real HTTP integration contract, with mocked transport only."""

import asyncio
import concurrent.futures
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from wang.app import create_app
from wang.commerce import Commerce, OrderInput, StockChange
from wang.commerce_api import support_context
from wang.store import Store

HEAD = {"X-Wang-Client": "web"}
ADMIN = HEAD | {"Authorization": "Bearer " + "a" * 40}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WANG_ADMIN_TOKEN", "a" * 40)
    with TestClient(create_app(tmp_path / "business.db")) as client:
        yield client


def stock(client, count=5):
    return client.post(
        "/api/admin/commerce/inventory",
        headers=ADMIN,
        json={"sku": "tee-m", "product_id": "tee-001", "delta": count, "expected_version": 0},
    )


def order_body(key="order-0001", source="native", items=None):
    return {
        "request_id": key,
        "source": source,
        "status": "awaiting_shipment",
        "external_id": key,
        "payment_reference": "verified-payment-record",
        "paid_cents": 5000,
        "items": items or [{"sku": "tee-m", "name": "Cotton M", "quantity": 2, "unit_cents": 2500}],
    }


def create(client, key="order-0001", source="native"):
    response = client.post("/api/admin/commerce/orders", headers=ADMIN, json=order_body(key, source))
    assert response.status_code == 200, response.text
    result = response.json()
    assert (
        client.post(
            "/api/commerce/access", headers=HEAD, json={"access_token": result["access_token"]}
        ).status_code
        == 200
    )
    return result


def refund(client, amount=2000, key="refund-0001"):
    response = client.post(
        "/api/commerce/refunds",
        headers=HEAD,
        json={"request_id": key, "amount_cents": amount, "reason": "Size does not fit"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_admin_and_buyer_boundaries(client, monkeypatch):
    assert stock(client).status_code == 200
    assert client.get("/api/admin/commerce/orders").status_code == 401
    assert client.post("/api/admin/commerce/inventory", headers=HEAD, json={}).status_code == 401
    assert client.get("/api/commerce/order").status_code == 404
    record = create(client)
    assert len(record["access_token"]) >= 32
    assert client.get("/api/commerce/order").json()["amount_cents"] == 5000
    with TestClient(client.app) as other:
        assert other.get("/api/commerce/order").status_code == 404
        assert (
            other.post("/api/commerce/access", headers=HEAD, json={"access_token": "x" * 40}).status_code
            == 404
        )
    new = client.post(f"/api/admin/commerce/orders/{record['id']}/access", headers=ADMIN, json={}).json()
    assert new["access_token"] != record["access_token"]
    assert client.get("/api/commerce/order").status_code == 404
    monkeypatch.delenv("WANG_ADMIN_TOKEN")
    assert client.get("/api/admin/commerce/orders", headers=ADMIN).status_code == 503


def test_atomic_stock_idempotency_and_version(client):
    assert stock(client).status_code == 200
    assert stock(client).status_code == 409
    create(client)
    assert client.get("/api/commerce/inventory/native/tee-m").json()["available"] == 3
    assert client.post("/api/admin/commerce/orders", headers=ADMIN, json=order_body()).status_code == 409
    items = [
        {"sku": "tee-m", "name": "Cotton", "quantity": 2, "unit_cents": 1000},
        {"sku": "missing", "name": "Missing", "quantity": 1, "unit_cents": 1000},
    ]
    assert (
        client.post(
            "/api/admin/commerce/orders", headers=ADMIN, json=order_body("order-0002", items=items)
        ).status_code
        == 409
    )
    assert client.get("/api/commerce/inventory/native/tee-m").json()["available"] == 3


def test_concurrent_stock_cannot_oversell(tmp_path):
    commerce = Commerce(Store(tmp_path / "stock.db"))
    commerce.adjust_stock(StockChange(sku="one", product_id="p", delta=1, expected_version=0))

    def buy(n):
        body = OrderInput.model_validate(
            order_body(
                f"order-{n:04}", items=[{"sku": "one", "name": "Only", "quantity": 1, "unit_cents": 1000}]
            )
        )
        try:
            commerce.create_order(body)
            return True
        except HTTPException as exc:
            assert exc.status_code == 409
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(buy, range(4))) == 1
    assert commerce.stock("p")[0]["available"] == 0


def test_shipment_refund_reservation_and_approval(client):
    stock(client)
    record = create(client)
    path = f"/api/admin/commerce/orders/{record['id']}/shipment"
    assert client.post(path, headers=ADMIN, json={"status": "shipped", "event": "Shipped"}).status_code == 422
    assert (
        client.post(
            path,
            headers=ADMIN,
            json={
                "status": "shipped",
                "carrier": "Carrier",
                "tracking_number": "TRACK123456",
                "event": "Collected",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(path, headers=ADMIN, json={"status": "awaiting_shipment", "event": "Oops"}).status_code
        == 409
    )
    first = refund(client, 4000)
    assert refund(client, 4000) == first
    assert (
        client.post(
            "/api/commerce/refunds",
            headers=HEAD,
            json={"request_id": "refund-0002", "amount_cents": 2000, "reason": "other"},
        ).status_code
        == 409
    )
    assert (
        client.post(f"/api/admin/commerce/refunds/{first}/execute", headers=ADMIN, json={}).status_code == 409
    )
    assert (
        client.post(
            f"/api/admin/commerce/refunds/{first}/decision", headers=HEAD, json={"approved": True}
        ).status_code
        == 401
    )
    client.post(f"/api/admin/commerce/refunds/{first}/decision", headers=ADMIN, json={"approved": False})
    assert refund(client, 2000, "refund-0002") != first
    assert len(client.get("/api/admin/commerce/audit", headers=ADMIN).json()["events"]) >= 4


def test_gateway_execution_unknown_then_reconciliation(client, monkeypatch):
    stock(client)
    create(client)
    refund_id = refund(client)
    base = f"/api/admin/commerce/refunds/{refund_id}"
    client.post(base + "/decision", headers=ADMIN, json={"approved": True})
    commerce = client.app.state.commerce
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append((args, kwargs))
        raise HTTPException(502, "timeout")

    monkeypatch.setattr(commerce, "gateway", unavailable)
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 502
    assert client.get("/api/commerce/order").json()["refunds"][0]["status"] == "uncertain"
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 409
    assert len(calls) == 1

    async def success(source, method, path, **kwargs):
        assert method == "GET" and path.endswith(refund_id)
        return {
            "refund_id": refund_id,
            "amount_cents": 2000,
            "currency": "CNY",
            "status": "succeeded",
            "receipt": "provider-123",
        }

    monkeypatch.setattr(commerce, "gateway", success)
    assert client.post(base + "/reconcile", headers=ADMIN, json={}).json()["status"] == "succeeded"
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 409
    assert (
        client.post(
            "/api/commerce/refunds",
            headers=HEAD,
            json={"request_id": "refund-large", "amount_cents": 4000, "reason": "additional"},
        ).status_code
        == 409
    )


def test_missing_gateway_never_succeeds_or_loses_approval(client, monkeypatch):
    monkeypatch.delenv("WANG_NATIVE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("WANG_NATIVE_GATEWAY_TOKEN", raising=False)
    stock(client)
    create(client)
    rid = refund(client)
    base = f"/api/admin/commerce/refunds/{rid}"
    client.post(base + "/decision", headers=ADMIN, json={"approved": True})
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 502
    assert client.get("/api/commerce/order").json()["refunds"][0]["status"] == "approved"


@pytest.mark.parametrize("source", ["taobao", "douyin"])
def test_platform_contract_and_no_manual_platform_shipment(client, monkeypatch, source):
    record = create(client, source=source)
    assert (
        client.post(
            f"/api/admin/commerce/orders/{record['id']}/shipment",
            headers=ADMIN,
            json={"status": "shipped", "event": "fake"},
        ).status_code
        == 409
    )
    commerce = client.app.state.commerce

    async def gateway(channel, method, path):
        assert channel == source
        return {
            "external_id": "order-0001",
            "status": "shipped",
            "carrier": "Courier",
            "tracking_number": "TRACK999",
            "event": "Platform collection",
        }

    monkeypatch.setattr(commerce, "gateway", gateway)
    refreshed = client.post("/api/commerce/order/refresh", headers=HEAD, json={})
    assert refreshed.status_code == 200
    assert refreshed.json()["shipment"]["platform_verified"]
    context = support_context(commerce, record["access_token"], "")
    encoded = json.dumps(context)
    assert "TRACK999" not in encoded and record["access_token"] not in encoded
    assert "verified-payment-record" not in encoded
    assert context["order"]["status"] == "shipped"


def test_http_bridge_payload_idempotency_and_response_validation(tmp_path, monkeypatch):
    commerce = Commerce(Store(tmp_path / "bridge.db"))
    monkeypatch.setenv("WANG_NATIVE_GATEWAY_URL", "https://merchant.example/v1")
    monkeypatch.setenv("WANG_NATIVE_GATEWAY_TOKEN", "unit-test-token")
    real_client = httpx.AsyncClient
    seen = []

    def handler(request):
        assert request.url == "https://merchant.example/v1/refunds"
        assert request.headers["Idempotency-Key"] == "refund-unit"
        assert request.headers["Authorization"] == "Bearer unit-test-token"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"received": True})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler))
    )
    assert asyncio.run(
        commerce.gateway("native", "POST", "/refunds", {"amount_cents": 12}, key="refund-unit")
    ) == {"received": True}
    assert seen == [{"amount_cents": 12}]


def test_mismatched_gateway_receipt_is_uncertain(client, monkeypatch):
    stock(client)
    create(client)
    rid = refund(client)
    base = f"/api/admin/commerce/refunds/{rid}"
    client.post(base + "/decision", headers=ADMIN, json={"approved": True})

    async def wrong(*args, **kwargs):
        return {
            "refund_id": rid,
            "amount_cents": 1,
            "currency": "CNY",
            "status": "succeeded",
            "receipt": "wrong",
        }

    monkeypatch.setattr(client.app.state.commerce, "gateway", wrong)
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 502
    assert client.get("/api/commerce/order").json()["refunds"][0]["status"] == "uncertain"


def test_model_context_uses_verified_binding_and_chat_cannot_mutate(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    stock(client)
    create(client)
    response = client.post(
        "/api/chat", headers=HEAD, json={"message": "帮我直接退款500元", "product_id": "tee-001"}
    )
    assert response.status_code == 200
    assert client.get("/api/commerce/order").json()["refunds"] == []
    assert client.get("/api/commerce/inventory/native/tee-m").json()["available"] == 3


def test_ledger_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("WANG_ADMIN_TOKEN", "a" * 40)
    path = tmp_path / "persistent.db"
    with TestClient(create_app(path)) as c:
        stock(c)
        record = create(c)
        refund(c)
    with TestClient(create_app(path)) as c:
        assert (
            c.post(
                "/api/commerce/access", headers=HEAD, json={"access_token": record["access_token"]}
            ).status_code
            == 200
        )
        assert len(c.get("/api/commerce/order").json()["refunds"]) == 1
        assert c.get("/api/commerce/inventory/native/tee-m").json()["available"] == 3


@pytest.mark.parametrize("source", ["native", "taobao", "douyin"])
def test_successful_execution_is_once_and_amount_is_actual_paid(client, monkeypatch, source):
    if source == "native":
        stock(client)
    create(client, source=source)
    rid = refund(client, 5000)
    base = f"/api/admin/commerce/refunds/{rid}"
    client.post(base + "/decision", headers=ADMIN, json={"approved": True})
    calls = []

    async def gateway(channel, method, path, body=None, key=None):
        assert channel == source and method == "POST" and path == "/refunds"
        assert key == rid and body["refund_id"] == rid and body["amount_cents"] == 5000
        assert body["payment_reference"] == "verified-payment-record"
        calls.append(key)
        return {
            "refund_id": rid,
            "amount_cents": 5000,
            "currency": "CNY",
            "status": "submitted",
            "receipt": "accepted-123",
        }

    monkeypatch.setattr(client.app.state.commerce, "gateway", gateway)
    assert client.post(base + "/execute", headers=ADMIN, json={}).json()["status"] == "submitted"
    assert client.post(base + "/execute", headers=ADMIN, json={}).status_code == 409
    assert calls == [rid]


def test_concurrent_refund_amount_is_reserved_atomically(tmp_path):
    from wang.commerce import RefundInput

    commerce = Commerce(Store(tmp_path / "refund-race.db"))
    commerce.adjust_stock(StockChange(sku="tee-m", product_id="p", delta=5, expected_version=0))
    record = commerce.create_order(OrderInput.model_validate(order_body()))

    def request(n):
        try:
            commerce.request_refund(
                record["access_token"],
                RefundInput(request_id=f"refund-{n:04}", amount_cents=4000, reason="size"),
            )
            return True
        except HTTPException as exc:
            assert exc.status_code == 409
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(request, range(2))) == 1


def test_order_business_context_reaches_model_without_credentials(client, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("WANG_MODE", "auto")
    stock(client)
    record = create(client)
    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": "订单已登记，待发货。"}}]}
    fake = AsyncMock()
    fake.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = fake
    monkeypatch.setattr("wang.agent.httpx.AsyncClient", lambda **kwargs: context)
    answer = client.post(
        "/api/chat", headers=HEAD, json={"message": "我的订单发货了吗", "product_id": "tee-001"}
    )
    assert answer.status_code == 200
    fake.post.assert_awaited_once()
    payload = fake.post.call_args.kwargs["json"]
    text = json.dumps(payload, ensure_ascii=False)
    assert "business_context" in text and "awaiting_shipment" in text and "5000" in text
    assert record["access_token"] not in text and "verified-payment-record" not in text
    assert '"available": 3' in text.replace('\\"', '"')
