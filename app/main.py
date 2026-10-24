"""FastAPI 앱. 경로는 /api/v1 아래에 두고, /health 만 루트에 둔다 (api.md 1.1).

실행: uvicorn app.main:create_app --factory (가져올 때 DB에 연결하지 않도록 팩토리로 띄운다)
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_sessionmaker
from app.errors import install_error_handlers
from app.push import PushSender, RecordingPushSender
from app.ratelimit import SlidingWindow
from app.reader import Reader, build_reader
from app.routers import account, dose, handoffs, pairing, photos, pillboxes


def create_app(
    settings: Settings | None = None,
    *,
    push: PushSender | None = None,
    layout_reader: Reader | None = None,
    cells_reader: Reader | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)

    app = FastAPI(title="woowang-server", version=__version__)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.push = push or RecordingPushSender()
    app.state.layout_reader = layout_reader or build_reader(settings.reader_layout, settings)
    app.state.cells_reader = cells_reader or build_reader(settings.reader_cells, settings)
    app.state.limits = {
        "families": SlidingWindow(10, timedelta(hours=1)),
        "pair_fail": SlidingWindow(
            settings.pair_fail_limit, timedelta(minutes=settings.pair_fail_window_min)
        ),
    }
    install_error_handlers(app)

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

    for module in (pairing, account, pillboxes, photos, dose, handoffs):
        app.include_router(module.router, prefix="/api/v1")
    return app
