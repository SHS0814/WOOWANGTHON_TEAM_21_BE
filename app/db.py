"""DB 연결. 운영은 PostgreSQL, 테스트는 SQLite 메모리 DB를 쓴다.

마이그레이션(Alembic)은 MVP 1~5 뒤에 붙인다 (R-007 권고). 지금은 시작할 때 테이블을 만든다.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """항상 UTC aware datetime 으로 저장·반환한다 (SQLite 는 시간대를 보관하지 않음)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite") and ":memory:" in url:
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


log = logging.getLogger(__name__)


def after_commit(db: Session, fn) -> None:
    """커밋이 성공한 뒤에 실행할 일 (푸시 발송 등). 롤백되면 버린다."""
    db.info.setdefault("after_commit", []).append(fn)


def _run_after_commit(session: Session) -> None:
    for fn in session.info.pop("after_commit", []):
        try:
            fn()
        except Exception:
            log.exception("after_commit hook failed")


def _drop_after_commit(session: Session) -> None:
    session.info.pop("after_commit", None)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event.listen(factory, "after_commit", _run_after_commit)
    event.listen(factory, "after_rollback", _drop_after_commit)
    return factory


def ensure_schema(engine: Engine) -> None:
    """테이블과 인덱스를 만든다. 이미 있는 DB에도 새 유니크 인덱스를 붙인다.

    Alembic 전까지의 임시 방편 (R-007 권고). 인덱스를 붙이기 전에 경합으로 생긴
    중복 행(열린 인계, 같은 칸·날 복약 건)을 정리한다.
    """
    Base.metadata.create_all(engine)
    existing = {i["name"] for i in inspect(engine).get_indexes("handoffs")}
    with engine.begin() as conn:
        if "uq_open_handoff_event_reason" not in existing:
            removed = conn.execute(
                text(
                    "DELETE FROM handoffs WHERE id IN ("
                    " SELECT h.id FROM handoffs h WHERE h.verdict IS NULL AND h.binds_event"
                    " AND EXISTS (SELECT 1 FROM handoffs o WHERE o.dose_event_id = h.dose_event_id"
                    " AND o.reason = h.reason AND o.verdict IS NULL AND o.binds_event"
                    " AND (o.notified_at < h.notified_at"
                    "  OR (o.notified_at = h.notified_at AND o.id < h.id))))"
                )
            ).rowcount
            if removed:
                log.warning("removed %d duplicate open handoffs before adding index", removed)
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            try:
                index.create(engine, checkfirst=True)
            except Exception:
                # 예: 예전 경합으로 같은 칸·날 복약 건이 이미 둘 있음. 서버는 띄우고 알린다
                log.exception("could not create index %s; check duplicate rows", index.name)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
