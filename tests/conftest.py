from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clock import clock
from app.config import Settings
from app.main import create_app
from app.push import RecordingPushSender
from app.reader import CellReading, CellReadings, Layout, MockReader, ReaderError
from app.timeutil import KST

SAMPLE_PNG = (
    Path(__file__).parent.parent / "harness" / "samples" / "synthetic-2x7.png"
).read_bytes()

# 2026-10-23 (금) 07:00 KST
T0 = datetime(2026, 10, 23, 7, 0, tzinfo=KST)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        photo_dir=str(tmp_path / "photos"),
        photo_url_secret="test-secret",
        public_base_url="https://test.example",
    )


@pytest.fixture
def now():
    clock.set(T0)
    yield T0
    clock.set(None)


@pytest.fixture
def push() -> RecordingPushSender:
    return RecordingPushSender()


class FakeReader:
    """칸 상태를 테스트에서 정하는 판독기. 정하지 않은 칸은 default. is_mock=True."""

    provider = "fake"
    model = "fake-v1"

    def __init__(self) -> None:
        self.default = "full"
        self.states: dict[str, str] = {}
        self.confidence: dict[str, float] = {}
        self.fail = False
        self.purposes: list[str] = []
        self._mock = MockReader()

    def detect_layout(self, image: bytes):
        return self._mock.detect_layout(image)

    def read_cells(self, image: bytes, layout: Layout, purpose: str = "dose") -> CellReadings:
        self.purposes.append(purpose)
        if self.fail:
            raise ReaderError("boom")
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=True,
            cells=[
                CellReading(
                    cell_id=cid,
                    state=self.states.get(cid, self.default),
                    confidence=self.confidence.get(cid, 0.95),
                )
                for cid in layout.cell_ids()
            ],
        )


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


@pytest.fixture
def client(settings, push, now, reader) -> TestClient:
    settings.jobs_enabled = False
    return TestClient(create_app(settings, push=push, cells_reader=reader))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class Family:
    """테스트용: 가족 만들기 → 어르신 연결 → 동의까지."""

    def __init__(self, client: TestClient, consent: bool = True, **family_kw) -> None:
        self.client = client
        res = client.post(
            "/api/v1/families",
            json={"elder_display_name": "어머니", "guardian_display_name": "민수", **family_kw},
        )
        assert res.status_code == 201, res.text
        self.family_id = res.json()["family_id"]
        self.g = bearer(res.json()["token"])
        code = client.post("/api/v1/pairing-codes", json={"kind": "elder"}, headers=self.g)
        res = client.post("/api/v1/pair", json={"code": code.json()["code"]})
        assert res.status_code == 201, res.text
        self.e = bearer(res.json()["token"])
        if consent:
            res = client.post(
                "/api/v1/consent",
                json={
                    "agreed": True,
                    "consent_text_version": "v1",
                    "ai_external_transfer_shown": False,
                },
                headers=self.e,
            )
            assert res.status_code == 200, res.text

    def register_pillbox(self, rows: int = 2, cols: int = 7) -> dict:
        res = self.client.post(
            "/api/v1/pillboxes/detect",
            files={"photo": ("box.png", SAMPLE_PNG, "image/png")},
            headers=self.g,
        )
        assert res.status_code == 200, res.text
        layout = {"rows": rows, "cols": cols, "row_labels": [], "col_labels": []}
        res = self.client.post(
            "/api/v1/pillboxes",
            json={"photo_id": res.json()["photo"]["id"], "layout": layout},
            headers=self.g,
        )
        assert res.status_code == 201, res.text
        return res.json()

    def put_schedule(self, slots: list[dict]) -> dict:
        res = self.client.put("/api/v1/schedule", json={"slots": slots}, headers=self.g)
        assert res.status_code == 200, res.text
        return res.json()


def week_schedule(times=(("morning", "08:00"), ("evening", "19:00"))) -> list[dict]:
    """행 = 시간대, 열 = 요일 (r{행}c{요일})."""
    return [
        {"weekday": wd, "time_of_day": tod, "dose_time": t, "cell_id": f"r{row}c{wd}"}
        for row, (tod, t) in enumerate(times)
        for wd in range(7)
    ]
