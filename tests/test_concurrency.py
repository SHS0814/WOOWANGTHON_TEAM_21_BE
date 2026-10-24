"""같은 원인의 인계가 동시 요청으로 두 번 생기지 않는지 (R-007 C2, R-018).

- SQLite: 두 세션이 같은 복약 건을 동시에 본 상황을 순서대로 재현한다
- PostgreSQL: TEST_DATABASE_URL 이 있으면 스레드로 실제 동시 실행을 확인한다
  (예: TEST_DATABASE_URL=postgresql+psycopg://woowang:woowang@127.0.0.1:55432/woowang_test)
"""

import os
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.clock import clock
from app.jobs import run_once
from app.main import create_app
from app.models import DoseEvent, Family, Handoff
from app.services import dose as ds
from app.timeutil import KST
from tests import conftest
from tests.conftest import week_schedule

DEADLINE = datetime(2026, 10, 23, 8, 31, tzinfo=KST)  # 금요일 아침 08:00 + 31분
SATURDAY = datetime(2026, 10, 24, 7, 0, tzinfo=KST)  # 아직 복약 건이 없는 날


def _setup(client) -> conftest.Family:
    f = conftest.Family(client)
    f.register_pillbox()
    f.put_schedule(week_schedule())
    return f


def _open_count(app, reason: str) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(
            select(func.count(Handoff.id)).where(
                Handoff.reason == reason, Handoff.verdict.is_(None)
            )
        )


@pytest.fixture
def file_client(tmp_path, settings, push, now, reader):
    settings.database_url = f"sqlite:///{tmp_path / 'race.sqlite3'}"
    settings.jobs_enabled = False
    return TestClient(create_app(settings, push=push, cells_reader=reader))


def test_two_sessions_racing_create_one_handoff(file_client, push):
    app = file_client.app
    _setup(file_client)
    clock.set(DEADLINE)
    with app.state.sessionmaker() as a, app.state.sessionmaker() as b:
        fam_a = a.scalar(select(Family))
        fam_b = b.get(Family, fam_a.id)
        ds.sync_events(a, fam_a.id, DEADLINE)
        a.commit()
        # 두 요청이 같은 순간에 같은 복약 건(아직 asked)을 읽었다
        ev_a = a.scalar(select(DoseEvent).where(DoseEvent.scheduled_at <= DEADLINE))
        ev_b = b.get(DoseEvent, ev_a.id)
        ev_a.state = ev_b.state = "asked"
        ds.advance_event(a, app.state.settings, app.state.push, fam_a, ev_a, DEADLINE)
        a.commit()
        ds.advance_event(b, app.state.settings, app.state.push, fam_b, ev_b, DEADLINE)
        b.commit()
    assert _open_count(app, "no_response") == 1
    assert [m.data["reason"] for m in push.sent] == ["no_response"]  # 푸시도 한 번


def test_unique_index_rejects_duplicate_open_handoff(file_client):
    app = file_client.app
    _setup(file_client)
    clock.set(DEADLINE)
    run_once(app)
    with app.state.sessionmaker() as db:
        h = db.scalar(select(Handoff))
        db.add(Handoff(family_id=h.family_id, dose_event_id=h.dose_event_id, reason=h.reason))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 판정이 끝난 인계는 막지 않는다 (나중에 같은 사유로 다시 열릴 수 있음)
        h.verdict = "acknowledged"
        db.add(Handoff(family_id=h.family_id, dose_event_id=h.dose_event_id, reason=h.reason))
        db.commit()


def test_push_not_sent_when_transaction_rolls_back(file_client, push):
    app = file_client.app
    _setup(file_client)
    with app.state.sessionmaker() as db:
        fam = db.scalar(select(Family))
        ev = ds.sync_events(db, fam.id, SATURDAY)[0]
        ds.create_handoff(db, app.state.push, fam, ev, "no_response", DEADLINE)
        db.rollback()
    assert push.sent == []


def test_get_requests_do_not_change_state(file_client, push):
    """GET 은 읽기만 한다. 기한이 지나도 예약 작업이 돌기 전에는 인계가 생기지 않는다."""
    f = _setup(file_client)
    clock.set(DEADLINE)
    for path in ("/api/v1/dose-events/today", "/api/v1/handoffs", "/api/v1/summaries?period=day"):
        assert file_client.get(path, headers=f.g).status_code == 200
    assert _open_count(file_client.app, "no_response") == 0 and push.sent == []
    run_once(file_client.app)
    assert _open_count(file_client.app, "no_response") == 1


def test_sync_events_is_idempotent_across_sessions(file_client):
    app = file_client.app
    _setup(file_client)
    with app.state.sessionmaker() as a, app.state.sessionmaker() as b:
        fam_id = a.scalar(select(Family.id))
        assert len(ds.sync_events(a, fam_id, SATURDAY)) == 2
        a.commit()
        # b 는 a 가 만들기 전 목록을 본 것처럼 다시 만들려 해도 중복이 생기지 않는다
        assert ds.sync_events(b, fam_id, SATURDAY) == []
        b.commit()
    with app.state.sessionmaker() as db:
        sat = select(func.count(DoseEvent.id)).where(DoseEvent.date == "2026-10-24")
        assert db.scalar(sat) == 2
        # 이미 읽은 목록이 낡았어도 같은 칸·날 복약 건은 DB 가 막는다
        ev = db.scalar(select(DoseEvent).where(DoseEvent.date == "2026-10-24"))
        db.add(
            DoseEvent(
                family_id=ev.family_id,
                slot_id=ev.slot_id,
                date=ev.date,
                scheduled_at=ev.scheduled_at,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL 없음")
def test_postgres_concurrent_jobs_create_one_handoff(tmp_path, push, now, reader, monkeypatch):
    from sqlalchemy import create_engine, text

    from app.config import Settings

    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    engine.dispose()
    settings = Settings(
        _env_file=None,
        database_url=url,
        photo_dir=str(tmp_path / "photos"),
        jobs_enabled=False,
        public_base_url="https://test.example",
    )
    client = TestClient(create_app(settings, push=push, cells_reader=reader))
    _setup(client)
    clock.set(DEADLINE)
    n = 4
    barrier = threading.Barrier(n)
    errors: list[Exception] = []
    # 경합을 확실히 만든다: 모든 스레드가 복약 건을 읽은 뒤에야 인계를 만들기 시작한다.
    # 잠금이 있으면 다른 스레드는 이 건을 건너뛰어 여기 오지 않으므로 기다림은 시간 초과로 끝난다
    inner = threading.Barrier(n)
    original = ds.create_handoff

    def racing_create_handoff(*args, **kwargs):
        try:
            inner.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        return original(*args, **kwargs)

    monkeypatch.setattr(ds, "create_handoff", racing_create_handoff)

    def worker():
        try:
            barrier.wait()
            run_once(client.app, DEADLINE)
        except Exception as e:  # noqa: BLE001 - 스레드 안 오류를 모아 본 스레드에서 확인
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert _open_count(client.app, "no_response") == 1
    assert [m.data.get("reason") for m in push.sent] == ["no_response"]
