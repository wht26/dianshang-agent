"""Local customer support and authenticated commerce service."""

from contextlib import asynccontextmanager
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
from .coordination import RedisControl
from .shop import Shop, ShopConfig
from .memory import Memory, MemoryFile, Preferences
from .commerce import Commerce
from .commerce_api import routes, support_context


def create_app(db_path=None, knowledge_dir=None, shop_path=None):
    store = Store(db_path or os.getenv("DATABASE_URL"))
    knowledge = Knowledge(knowledge_dir or os.getenv("WANG_KNOWLEDGE_DIR"), store.dsn)
    shop = Shop(shop_path or os.getenv("WANG_SHOP_FILE"))
    store.initialize("CREATE TABLE IF NOT EXISTS shop_settings (id INTEGER PRIMARY KEY, payload TEXT)")
    with store.db() as db:
        saved_shop = db.execute("SELECT payload FROM shop_settings WHERE id=1").fetchone()
    if saved_shop and not shop_path and not os.getenv("WANG_SHOP_FILE"):
        import json
        shop = Shop(data=json.loads(saved_shop["payload"]))
    commerce = Commerce(store)
    agent = Agent(knowledge, shop)
    memory = Memory(store)
    control = RedisControl()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            control.close()

    app = FastAPI(title="wang · 服装鞋包客服 Agent", version="0.10.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.agent, app.state.store = agent, store
    app.state.commerce = commerce
    app.state.control = control
    app.include_router(routes(commerce))

    def refresh_runtime():
        nonlocal shop
        if not shop_path and not os.getenv("WANG_SHOP_FILE"):
            with store.db() as db:
                row = db.execute("SELECT payload FROM shop_settings WHERE id=1").fetchone()
            if row:
                import json
                shop = Shop(data=json.loads(row["payload"]))
                agent.shop = shop
        agent.knowledge = Knowledge(knowledge.extra_dir, store.dsn)

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

    @app.get("/health")
    async def health():
        try:
            with store.db() as db:
                db.execute("SELECT 1")
            await control.call(control.client.ping)
        except Exception:
            raise HTTPException(503, "PostgreSQL或Redis服务尚未就绪。") from None
        return {"status": "ok", "database": "postgresql", "coordination": "redis"}

    @app.get("/api/bootstrap")
    def bootstrap(request: Request, response: Response):
        refresh_runtime()
        token = session(request, response)
        owner(request, response)
        return {"name": "wang", "version": "0.10.0", "mode": agent.mode,
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash") if agent.mode == "deepseek" else None,
                "documents": agent.knowledge.documents, "history": store.history(token), "live_web": False,
                "shop": {"name": shop.config.name, "is_demo": shop.config.is_demo}, "products": shop.catalog()}

    @app.post("/api/shop/import")
    async def import_shop(body: ShopConfig):
        nonlocal shop
        if await control.busy():
            raise HTTPException(429, "请等待当前回答完成。")
        if shop_path or os.getenv("WANG_SHOP_FILE"):
            raise HTTPException(409, "当前使用外部店铺文件，请更新该文件并重启，或取消文件配置后在此导入。")
        try:
            replacement = Shop(data=body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with store.db() as db:
            db.execute("INSERT INTO shop_settings VALUES (1,%s) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload", (body.model_dump_json(),))
        shop = replacement
        agent.shop = replacement
        return {"name": shop.config.name, "products": shop.catalog()}

    @app.post("/api/conversations/new")
    async def new_conversation(response: Response):
        if await control.busy():
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
        refresh_runtime()
        return {"documents": agent.knowledge.documents, "chunks": len(agent.knowledge.chunks)}

    @app.post("/api/knowledge/import")
    async def import_document(body: DocumentInput):
        if await control.busy():
            raise HTTPException(429, "请等待回答完成后再更新知识库。")
        if body.product_id and body.product_id not in shop.products:
            raise HTTPException(422, "请选择有效商品。")
        try:
            doc_id = agent.knowledge.import_document(body.title, body.content, body.product_id)
            agent.knowledge = Knowledge(knowledge.extra_dir, store.dsn)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"id": doc_id, "documents": agent.knowledge.documents}

    @app.post("/api/knowledge/{doc_id}/remove")
    async def remove_document(doc_id: str):
        if await control.busy():
            raise HTTPException(429, "请等待回答完成后再更新知识库。")
        try:
            agent.knowledge.remove_document(doc_id)
            agent.knowledge = Knowledge(knowledge.extra_dir, store.dsn)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"documents": agent.knowledge.documents}

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request, response: Response):
        question = body.message.strip()
        if not question:
            raise HTTPException(422, "请输入问题。")
        if await control.busy():
            raise HTTPException(429, "正在生成回答，请稍后重试。")
        identity = store.digest(request.client.host if request.client else "unknown")
        await control.allow(identity)
        token = session(request, response)
        async with control.hold() as lease:
            try:
                refresh_runtime()
                preferences = memory.read(owner(request, response))
                business = support_context(commerce, request.cookies.get("wang_order"), body.product_id)
                result = await agent.answer(question, store.history(token), body.product_id, preferences, business)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            except ProviderError as exc:
                raise HTTPException(502, str(exc)) from None
            await lease.ensure_owned()
            store.append(token, question, result)
        return result

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/docs", include_in_schema=False)
    def api_docs():
        return FileResponse(static / "api.html")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app

