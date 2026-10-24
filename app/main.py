"""FastAPI 앱. 지금은 /health 만 있다. 엔드포인트는 API 계약서 승인 뒤 구현한다."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="woowang-server", version=__version__)

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

    return app


app = create_app()
