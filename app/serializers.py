"""DB 객체 → API 응답 (api.md 2장 공통 객체)."""

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device, Pillbox, Reading, Refill, ScheduleSlot
from app.storage import photo_out
from app.timeutil import iso


def base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def device_out(device: Device, me_id: str | None = None) -> dict:
    return {
        "id": device.id,
        "role": device.role,
        "display_name": device.display_name,
        "platform": device.platform,
        "paired_at": iso(device.paired_at),
        "consent_at": iso(device.consent_at),
        "last_seen_at": iso(device.last_seen_at),
        "is_me": device.id == me_id,
    }


def person(device: Device | None) -> dict | None:
    return None if device is None else {"device_id": device.id, "display_name": device.display_name}


def active_guardians(db: Session, family_id: str) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(
                Device.family_id == family_id,
                Device.role == "guardian",
                Device.revoked_at.is_(None),
            )
            .order_by(Device.paired_at)
        )
    )


def slot_out(slot: ScheduleSlot) -> dict:
    return {
        "id": slot.id,
        "weekday": slot.weekday,
        "time_of_day": slot.time_of_day,
        "dose_time": slot.dose_time,
        "cell_id": slot.cell_id,
    }


def pillbox_out(pb: Pillbox) -> dict:
    return {
        "id": pb.id,
        "layout": pb.layout,
        "registered_photo_id": pb.registered_photo_id,
        "confirmed_at": iso(pb.confirmed_at),
        "active": pb.active,
    }


def reading_out(db: Session, settings: Settings, url_base: str, reading: Reading) -> dict:
    from app.models import Photo

    photo = db.get(Photo, reading.photo_id)
    return {
        "id": reading.id,
        "photo": photo_out(settings, url_base, photo),
        "is_mock": reading.is_mock,
        "provider": reading.provider,
        "model": reading.model,
        "created_at": iso(reading.created_at),
        "target_cell_id": reading.target_cell_id,
        "classification": reading.classification,
        "cells": reading.cells,
    }


def refill_out(db: Session, settings: Settings, url_base: str, refill: Refill) -> dict:
    filled_by = db.get(Device, refill.filled_by_device_id)
    reading = db.get(Reading, refill.baseline_reading_id)
    unclear = {c["cell_id"] for c in reading.cells if c["ai_state"] == "unclear"}
    needs_review = any(
        c["cell_id"] in unclear and not c.get("edited") for c in refill.baseline_cells
    )
    return {
        "id": refill.id,
        "pillbox_id": refill.pillbox_id,
        "filled_at": iso(refill.filled_at),
        "filled_by": person(filled_by),
        "source": refill.source,
        "baseline_reading": reading_out(db, settings, url_base, reading),
        "baseline_cells": refill.baseline_cells,
        "needs_review": needs_review,
    }
