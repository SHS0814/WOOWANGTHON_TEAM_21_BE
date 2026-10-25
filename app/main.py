"""FastAPI 앱. 경로는 /api/v1 아래에 두고, /health 만 루트에 둔다 (api.md 1.1).

실행: uvicorn app.main:create_app --factory (가져올 때 DB에 연결하지 않도록 팩토리로 띄운다)
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, jobs
from app.config import Settings, get_settings
from app.db import ensure_schema, make_engine, make_sessionmaker
from app.errors import install_error_handlers
from app.push import ExpoPushSender, PushSender, RecordingPushSender
from app.ratelimit import SlidingWindow
from app.reader import Reader, build_server_reader
from app.routers import account, dose, handoffs, pairing, photos, pillboxes, summaries


def create_app(
    settings: Settings | None = None,
    *,
    push: PushSender | None = None,
    layout_reader: Reader | None = None,
    cells_reader: Reader | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    ensure_schema(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(jobs.loop(app)) if settings.jobs_enabled else None
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="woowang-server", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    if push is None:
        push = (
            ExpoPushSender(settings.expo_access_token)
            if settings.expo_push_enabled
            else RecordingPushSender()
        )
    app.state.push = push
    app.state.summary_sent = set()
    # family_id → 다음 복약 사진 1장의 모의 판독 시나리오 (QA, R-018)
    app.state.mock_scenarios = {}
    app.state.layout_reader = layout_reader or build_server_reader(settings.reader_layout, settings)
    app.state.cells_reader = cells_reader or build_server_reader(settings.reader_cells, settings)
    app.state.limits = {
        "families": SlidingWindow(10, timedelta(hours=1)),
        "pair_fail": SlidingWindow(
            settings.pair_fail_limit, timedelta(minutes=settings.pair_fail_window_min)
        ),
    }
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,  # 쿠키 없이 Bearer 토큰만 쓴다
        max_age=600,
    )

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "time": datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
            "reader": {
                "layout": settings.reader_layout,
                "cells": settings.reader_cells,
                "is_mock": settings.reader_is_mock,
            },
        }

    for module in (pairing, account, pillboxes, photos, dose, handoffs, summaries):
        app.include_router(module.router, prefix="/api/v1")
    return app
