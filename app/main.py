from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .database import create_engine_and_sessionmaker, get_db_path, get_db_url
from .models import Base
from .routers import admin as admin_router
from .routers import api as api_router
from . import rate_limit


def _log_level() -> str:
    return os.getenv("LOG_LEVEL", "info")


def create_app() -> FastAPI:
    app = FastAPI(title="Docker Log Gateway", docs_url=None, redoc_url=None)

    logging.basicConfig(level=_log_level().upper())

    db_url = get_db_url()
    db_path = get_db_path()
    engine, SessionLocal = create_engine_and_sessionmaker(db_url, db_path)
    app.state.engine = engine
    app.state.SessionLocal = SessionLocal
    app.state.templates = Jinja2Templates(
        directory=os.path.join(os.path.dirname(__file__), "templates")
    )
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
        name="static",
    )

    @app.on_event("startup")
    def _startup() -> None:
        Base.metadata.create_all(bind=engine)

        # In-memory rate limiting (single-instance).
        app.state.api_limiter = rate_limit.api_limiter()
        app.state.admin_login_limiter = rate_limit.admin_login_limiter()

        # Initialize Coolify DB connection
        from .coolify_db import coolify_db

        connected, message = coolify_db.initialize()
        if connected:
            logging.getLogger("log-gateway").info(f"Coolify DB: {message}")
        else:
            logging.getLogger("log-gateway").warning(f"Coolify DB: {message}")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        try:
            from .coolify_db import coolify_db

            coolify_db.stop()
        except Exception:
            pass
        try:
            engine.dispose()
        except Exception:
            pass

    def _is_api(request: Request) -> bool:
        return request.url.path.startswith("/api")

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException):
        if (
            exc.status_code == 401
            and request.url.path.startswith("/admin")
            and request.url.path not in ("/admin/login",)
        ):
            return RedirectResponse(url="/admin/login", status_code=303)
        if _is_api(request):
            return JSONResponse(
                status_code=exc.status_code, content={"error": str(exc.detail)}
            )
        # For admin/user pages, keep it simple and avoid stack traces.
        templates: Jinja2Templates = app.state.templates
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": str(exc.detail), "status": exc.status_code},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exc_handler(request: Request, exc: Exception):
        logging.getLogger("log-gateway").exception("Unhandled error: %s", exc)
        if _is_api(request):
            return JSONResponse(
                status_code=500, content={"error": "Internal server error"}
            )
        templates: Jinja2Templates = app.state.templates
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Internal server error", "status": 500},
            status_code=500,
        )

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if not rate_limit.enabled():
            return await call_next(request)

        path = request.url.path
        peer = request.client.host if request.client else None
        ip = rate_limit.client_ip(peer, dict(request.headers))

        if path.startswith("/api"):
            res = app.state.api_limiter.allow(ip)
            if not res.allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(res.retry_after_seconds)},
                    content={"error": "Rate limit exceeded"},
                )

        if path == "/admin/login" and request.method.upper() == "POST":
            res = app.state.admin_login_limiter.allow(ip)
            if not res.allowed:
                templates: Jinja2Templates = app.state.templates
                r = templates.TemplateResponse(
                    "admin_login.html",
                    {
                        "request": request,
                        "error": "Too many attempts, try again later.",
                    },
                    status_code=429,
                )
                r.headers["Retry-After"] = str(res.retry_after_seconds)
                return r

        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        templates: Jinja2Templates = app.state.templates
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/logs/{container_name}", response_class=HTMLResponse)
    def user_logs_page(request: Request, container_name: str):
        from .routers.api import CONTAINER_RE

        if not CONTAINER_RE.fullmatch(container_name or ""):
            raise HTTPException(status_code=400, detail="Invalid container name")
        templates: Jinja2Templates = app.state.templates
        return templates.TemplateResponse(
            "logs.html",
            {
                "request": request,
                "container_name": container_name,
                "ws_url": f"/api/logs/{container_name}",
                "require_token": True,
            },
        )

    # Routers
    app.include_router(api_router.router)
    app.include_router(admin_router.router)

    return app


app = create_app()
