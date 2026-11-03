"""Validated, replaceable store facts for buyer-facing support."""

import os
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    price_cents: int | None = Field(default=None, strict=True, ge=0)
    specs: dict[str, str] = Field(default_factory=dict)
    faq: dict[str, str] = Field(default_factory=dict)
    after_sales: str = Field(default="", max_length=5000)


class ShopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    is_demo: bool = False
    policies: dict[str, str] = Field(default_factory=dict)
    products: list[Product] = Field(default_factory=list, max_length=100)


class Shop:
    def __init__(self, path=None, data=None):
        path = path or os.getenv("WANG_SHOP_FILE")
        if data is None and path:
            path = Path(path)
            if path.stat().st_size > 1_000_000:
                raise ValueError("店铺资料不得超过1MB。")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        self.config = ShopConfig.model_validate(data or {"name": "服装店智能客服", "is_demo": False, "products": []})
        ids = [product.id for product in self.config.products]
        if len(ids) != len(set(ids)):
            raise ValueError("商品ID不能重复。")
        self.products = {product.id: product for product in self.config.products}

    def catalog(self):
        return [{"id": p.id, "name": p.name} for p in self.config.products]

    def context(self, question, history, product_id=None):
        if product_id and product_id not in self.products:
            raise ValueError("商品不存在，请重新选择。")
        # Explicit product mentions take precedence over a previous selection.
        matched = [p for p in self.config.products if any(
            alias and alias.casefold() in question.casefold() for alias in [p.id, p.name, *p.aliases])]
        if not matched:
            active_id = product_id
            if product_id is None and history:
                active_id = history[-1].get("product_id")
            matched = [self.products[active_id]] if active_id in self.products else []
        active_id = matched[0].id if len(matched) == 1 else None
        facts = []
        for index, product in enumerate(self.config.products, 1):
            if product not in matched:
                continue
            data = product.model_dump(exclude={"aliases", "price_cents"})
            data["price"] = f"{product.price_cents / 100:.2f}元（资料价，非实时成交价）" if product.price_cents is not None else "未提供"
            facts.append({"id": f"P{index:03}", "title": product.name + " · 商品资料", "content": data})
        policies = {"id": "S001", "title": self.config.name + " · 店铺政策", "content": self.config.policies}
        return {"shop": self.config.name, "is_demo": self.config.is_demo, "catalog": self.catalog(),
                "product_id": active_id, "product_facts": facts, "policy": policies}
