"""Authenticated merchant operations; buyer access is an order-scoped bearer capability."""

import os
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPBearer
from pydantic import Field

from .commerce import Input, OrderInput, RefundInput, Shipment, StockChange


class AccessInput(Input):
    access_token: str = Field(min_length=32, max_length=100)


class Decision(Input):
    approved: bool = Field(strict=True)


bearer = HTTPBearer(auto_error=False)


def client_header(x_wang_client: str = Header(default="web")):
    pass  # Enforced by the application middleware; included here for OpenAPI clients.


def admin(credentials=Depends(bearer)):
    configured = os.getenv("WANG_ADMIN_TOKEN", "")
    if len(configured) < 32:
        raise HTTPException(503, "需先通过环境变量设置至少32字符的管理员令牌。")
    supplied = "Bearer " + credentials.credentials if credentials else ""
    if not secrets.compare_digest(supplied.encode(), ("Bearer " + configured).encode()):
        raise HTTPException(401, "管理员身份校验失败。")


def routes(commerce):
    router = APIRouter(dependencies=[Depends(client_header)])
    merchant = APIRouter(prefix="/api/admin/commerce", dependencies=[Depends(admin)], tags=["商家业务"])
    buyer = APIRouter(prefix="/api/commerce", tags=["买家业务"])

    @merchant.get("/orders")
    def orders():
        with commerce.store.db() as db:
            return {
                "orders": [
                    commerce.summary(db, r)
                    for r in db.execute("SELECT * FROM commerce_orders ORDER BY created DESC LIMIT 100")
                ]
            }

    @merchant.get("/audit")
    def audit():
        with commerce.store.db() as db:
            return {
                "events": [
                    dict(r) for r in db.execute("SELECT * FROM commerce_audit ORDER BY id DESC LIMIT 100")
                ]
            }

    @merchant.get("/channels")
    def channels():
        return {
            "channels": [
                {
                    "source": s,
                    "local_ledger": s == "native",
                    "gateway_configured": bool(
                        os.getenv(f"WANG_{s.upper()}_GATEWAY_URL")
                        and os.getenv(f"WANG_{s.upper()}_GATEWAY_TOKEN")
                    ),
                    "platform_authorization_verified": False,
                }
                for s in ("native", "taobao", "douyin")
            ]
        }

    @merchant.post("/inventory")
    def inventory(body: StockChange):
        return {"items": commerce.adjust_stock(body)}

    @merchant.post("/orders")
    def create_order(body: OrderInput):
        return commerce.create_order(body)

    @merchant.post("/orders/{order_id}/access")
    def rotate(order_id: str):
        return commerce.rotate(order_id)

    @merchant.post("/orders/{order_id}/shipment")
    def shipment(order_id: str, body: Shipment):
        return commerce.shipment(order_id, body)

    @merchant.post("/refunds/{refund_id}/decision")
    def decision(refund_id: str, body: Decision):
        return commerce.decide(refund_id, body.approved)

    @merchant.post("/refunds/{refund_id}/execute")
    async def execute(refund_id: str):
        return await commerce.execute(refund_id)

    @merchant.post("/refunds/{refund_id}/reconcile")
    async def reconcile(refund_id: str):
        return await commerce.execute(refund_id, reconcile=True)

    @buyer.post("/access")
    def bind(body: AccessInput, response: Response):
        order = commerce.order(body.access_token)
        response.set_cookie("wang_order", body.access_token, httponly=True, samesite="strict", max_age=86400)
        return order

    @buyer.post("/access/clear")
    def clear(response: Response):
        response.delete_cookie("wang_order")
        return {"cleared": True}

    @buyer.get("/order")
    def order(request: Request):
        return commerce.order(request.cookies.get("wang_order"))

    @buyer.post("/order/refresh")
    async def refresh(request: Request):
        return await commerce.refresh(request.cookies.get("wang_order"))

    @buyer.post("/refunds")
    def request_refund(body: RefundInput, request: Request):
        return commerce.request_refund(request.cookies.get("wang_order"), body)

    @buyer.get("/inventory/{source}/{sku}")
    async def stock(source: Literal["native", "taobao", "douyin"], sku: str):
        import re

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", sku):
            raise HTTPException(422, "SKU格式不正确。")
        if source == "native":
            with commerce.store.db() as db:
                row = db.execute("SELECT * FROM commerce_stock WHERE sku=%s", (sku,)).fetchone()
                if not row:
                    raise HTTPException(404, "SKU未录入。")
                return dict(row) | {"source": source}
        from urllib.parse import quote

        data = await commerce.gateway(source, "GET", "/inventory/" + quote(sku, safe=""))
        if (
            not isinstance(data, dict)
            or data.get("sku") != sku
            or type(data.get("available")) is not int
            or not 0 <= data["available"] <= 100000000
        ):
            raise HTTPException(502, "平台库存响应校验失败。")
        return {"source": source, "sku": sku, "available": data["available"]}

    router.include_router(merchant)
    router.include_router(buyer)
    return router


def support_context(commerce, token, product_id):
    result = {"order": None, "inventory": commerce.stock(product_id) if product_id else []}
    if token:
        try:
            order = commerce.order(token)
        except HTTPException:
            return result
        # Exclude access credentials, payment references, tracking numbers and free-text notes from the model.
        result["order"] = {k: order[k] for k in ("source", "status", "currency", "amount_cents", "items")}
        result["order"]["shipment"] = {
            k: v for k, v in order["shipment"].items() if k in ("status", "carrier", "updated_at")
        }
        result["order"]["refunds"] = [{k: r[k] for k in ("amount_cents", "status")} for r in order["refunds"]]
        result["order"]["freshness"] = "本地记录；平台订单需在订单面板刷新后核验"
    return result
