"""Explicit customer preferences, separate from conversations and shop knowledge."""

import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fit: str = Field(default="", max_length=100)
    colors: str = Field(default="", max_length=100)
    materials: str = Field(default="", max_length=100)
    reply_style: str = Field(default="", max_length=100)


class MemoryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
    preferences: Preferences


class Memory:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS customer_memory (owner TEXT PRIMARY KEY, payload TEXT, updated REAL)")

    def read(self, token):
        with self.store.db() as db:
            row = db.execute("SELECT payload FROM customer_memory WHERE owner=?", (self.store.digest(token),)).fetchone()
        return json.loads(row[0]) if row else Preferences().model_dump()

    def write(self, token, preferences):
        data = Preferences.model_validate(preferences).model_dump()
        for value in data.values():
            if re.search(r"sk-[\w-]{12,}|1[3-9]\d{9}|\d{16,}", value):
                raise ValueError("请勿在偏好记忆中保存密钥、手机号或长账号。")
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO customer_memory VALUES (?,?,?)",
                       (self.store.digest(token), json.dumps(data, ensure_ascii=False), time.time()))
        return data

    def clear(self, token):
        with self.store.db() as db:
            db.execute("DELETE FROM customer_memory WHERE owner=?", (self.store.digest(token),))


def short_memory(history, budget=16000):
    """Bounded recent turns; older user statements remain labeled and unverified."""
    recent, used = [], 0
    for turn in reversed(history[-8:]):
        entry = {**turn, "question": turn["question"][:2500], "answer": turn["answer"][:3500]}
        cost = len(entry["question"]) + len(entry["answer"])
        if used + cost > budget:
            break
        recent.insert(0, entry)
        used += cost
    older = history[:-len(recent)] if recent else history
    notes = [turn["question"][:160] for turn in older[-4:]]
    return {"recent": recent, "earlier_user_statements": notes,
            "notice": "较早用户表述为截断摘录，可能已过时，不能作为商品事实或当前意愿。"}
