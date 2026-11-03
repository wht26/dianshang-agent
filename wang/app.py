"""Local Q&A-only service; no account or commerce endpoints."""

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent, ProviderError
from .knowledge import Knowledge
from .store import Store
from .shop import Shop, ShopConfig
from .memory import Memory, MemoryFile, Preferences


def create_app(db_path=None, knowledge_dir=None, shop_path=None):
    store = Store(db_path or os.getenv("WANG_QA_DB", "data/wang-support.db"))
    knowledge = Knowledge(knowledge_dir or os.getenv("WANG_KNOWLEDGE_DIR"), store.path)
    shop = Shop(shop_path or os.getenv("WANG_SHOP_FILE"))
    with store.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS shop_settings (id INTEGER PRIMARY KEY, payload TEXT)")
        saved_shop = db.execute("SELECT payload FROM shop_settings WHERE id=1").fetchone()
    if saved_shop and not shop_path and not os.getenv("WANG_SHOP_FILE"):
        import json
        shop = Shop(data=json.loads(saved_shop[0]))
    agent = Agent(knowledge, shop)
    memory = Memory(store)
    lock = asyncio.Lock()
    app = FastAPI(title="wang · 服装鞋包客服 Agent", version="0.8.0")
    app.state.agent, app.state.store = agent, store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if int(request.headers.get("content-length", "0")) > 650_000:
                return JSONResponse({"detail": "请求过大。"}, status_code=413)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨来源请求。"}, status_code=403)
            if request.headers.get("x-wang-client") != "web":
                return JSONResponse({"detail": "缺少客户端校验头。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def session(request, response):
        token = store.session(request.cookies.get("wang_support"))
        if token != request.cookies.get("wang_support"):
            response.set_cookie("wang_support", token, httponly=True, samesite="strict", max_age=30 * 86400)
        return token

    def owner(request, response):
        token = store.session(request.cookies.get("wang_memory"))
        if token != request.cookies.get("wang_memory"):
            response.set_cookie("wang_memory", token, httponly=True, samesite="strict", max_age=30 * 86400)
        return token

    @app.get("/api/memory")
    def read_memory(request: Request, response: Response):
        return {"schema_version": 1, "preferences": memory.read(owner(request, response))}

    @app.post("/api/memory")
    def write_memory(body: Preferences, request: Request, response: Response):
        try:
            return {"preferences": memory.write(owner(request, response), body.model_dump())}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/memory/import")
    def import_memory(body: MemoryFile, request: Request, response: Response):
        try:
            return {"preferences": memory.write(owner(request, response), body.preferences.model_dump())}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/memory/clear")
    def clear_memory(request: Request, response: Response):
        memory.clear(owner(request, response))
        return {"preferences": Preferences().model_dump()}

    @app.get("/api/bootstrap")
    def bootstrap(request: Request, response: Response):
        token = session(request, response)
        owner(request, response)
        return {"name": "wang", "version": "0.8.0", "mode": agent.mode,
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash") if agent.mode == "deepseek" else None,
                "documents": agent.knowledge.documents, "history": store.history(token), "live_web": False,
                "shop": {"name": shop.config.name, "is_demo": shop.config.is_demo}, "products": shop.catalog()}

    @app.post("/api/shop/import")
    async def import_shop(body: ShopConfig):
        nonlocal shop
        if lock.locked():
            raise HTTPException(429, "请等待当前回答完成。")
        if shop_path or os.getenv("WANG_SHOP_FILE"):
            raise HTTPException(409, "当前使用外部店铺文件，请更新该文件并重启，或取消文件配置后在此导入。")
        try:
            replacement = Shop(data=body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with store.db() as db:
            db.execute("INSERT OR REPLACE INTO shop_settings VALUES (1,?)", (body.model_dump_json(),))
        shop = replacement
        agent.shop = replacement
        return {"name": shop.config.name, "products": shop.catalog()}

    @app.post("/api/conversations/new")
    async def new_conversation(response: Response):
        if lock.locked():
            raise HTTPException(429, "请等待当前回答完成后再开启新对话。")
        token = store.session(None)
        response.set_cookie("wang_support", token, httponly=True, samesite="strict", max_age=30 * 86400)
        return {"history": []}

    class ChatInput(BaseModel):
        message: str = Field(min_length=1, max_length=6000)
        product_id: str | None = Field(default=None, max_length=60)

    class DocumentInput(BaseModel):
        title: str = Field(min_length=1, max_length=120)
        content: str = Field(min_length=1, max_length=100_000)
        product_id: str = Field(default="", max_length=60)

    @app.get("/api/knowledge")
    def documents():
        return {"documents": agent.knowledge.documents, "chunks": len(agent.knowledge.chunks)}

    @app.post("/api/knowledge/import")
    async def import_document(body: DocumentInput):
        if lock.locked():
            raise HTTPException(429, "请等待回答完成后再更新知识库。")
        if body.product_id and body.product_id not in shop.products:
            raise HTTPException(422, "请选择有效商品。")
        try:
            doc_id = agent.knowledge.import_document(body.title, body.content, body.product_id)
            agent.knowledge = Knowledge(knowledge.extra_dir, store.path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"id": doc_id, "documents": agent.knowledge.documents}

    @app.post("/api/knowledge/{doc_id}/remove")
    async def remove_document(doc_id: str):
        if lock.locked():
            raise HTTPException(429, "请等待回答完成后再更新知识库。")
        try:
            agent.knowledge.remove_document(doc_id)
            agent.knowledge = Knowledge(knowledge.extra_dir, store.path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"documents": agent.knowledge.documents}

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request, response: Response):
        question = body.message.strip()
        if not question:
            raise HTTPException(422, "请输入问题。")
        if lock.locked():
            raise HTTPException(429, "正在生成回答，请稍后重试。")
        token = session(request, response)
        async with lock:
            try:
                preferences = memory.read(owner(request, response))
                result = await agent.answer(question, store.history(token), body.product_id, preferences)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            except ProviderError as exc:
                raise HTTPException(502, str(exc)) from None
            store.append(token, question, result)
        return result

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app


app = create_app()
