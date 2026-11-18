"""HTTP-приёмник вебхука Авито."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import Settings
from app.service import Service


def authorized(provided: str, secret: str) -> bool:
    if not secret:
        return False
    try:
        return secrets.compare_digest(provided, secret)
    except (TypeError, ValueError):
        return False


def create_app(
    settings: Settings | None = None,
    service: Service | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    owns_service = service is None
    service = service or Service(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_service:
            await service.start()
        yield
        if owns_service:
            await service.stop()

    app = FastAPI(title="Avito Telegram notifier", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/avito/webhook")
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        token: str = "",
    ) -> dict[str, bool]:
        if not authorized(token, settings.webhook_secret):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            body: Any = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected object")
        background.add_task(service.handle_webhook, body)
        return {"ok": True}

    return app
