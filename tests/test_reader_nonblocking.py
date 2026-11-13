"""판독(외부 AI 호출)이 진행되는 동안 다른 요청이 멈추지 않는지.

판독은 5~20초 걸리는 동기 호출이다. 사진을 받는 핸들러가 이벤트 루프에서 판독을 부르면
워커가 하나라 그동안 보호자 요청과 /health 가 모두 멈춘다.
"""

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.jobs import run_once
from app.main import create_app
from tests import conftest
from tests.conftest import SAMPLE_PNG, FakeReader, week_schedule

READ_SECONDS = 1.0


class SlowReader(FakeReader):
    """판독이 오래 걸리는 판독기. 판독에 들어가면 started 를 켠다."""

    def __init__(self) -> None:
        super().__init__()
        self.slow = False
        self.started = threading.Event()

    def _wait(self) -> None:
        if self.slow:
            self.started.set()
            time.sleep(READ_SECONDS)

    def detect_layout(self, image: bytes):
        self._wait()
        return super().detect_layout(image)

    def read_cells(self, image, layout, purpose="dose"):
        self._wait()
        return super().read_cells(image, layout, purpose)


@pytest.fixture
def live(tmp_path, settings, push, now):
    """실제 서버처럼 이벤트 루프 하나로 띄운다.

    TestClient 는 요청마다 루프를 따로 써서 루프가 막히는 문제가 드러나지 않는다.
    """
    settings.jobs_enabled = False
    settings.database_url = f"sqlite:///{tmp_path / 'live.db'}"
    reader = SlowReader()
    app = create_app(settings, push=push, cells_reader=reader, layout_reader=reader)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "서버가 뜨지 않음"
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
        yield client, reader, app
    server.should_exit = True
    thread.join(10)


def _health_while(client: httpx.Client, reader: SlowReader, request) -> float:
    """request 가 판독에 들어간 뒤 /health 가 응답하는 데 걸린 시간(초)."""
    result = {}

    def run():
        result["res"] = request()

    reader.slow = True
    t = threading.Thread(target=run)
    t.start()
    assert reader.started.wait(5), "판독이 시작되지 않음"
    began = time.monotonic()
    res = client.get("/health")
    elapsed = time.monotonic() - began
    t.join(10)
    reader.slow = False
    assert res.status_code == 200
    assert result["res"].status_code in (200, 201), result["res"].text
    return elapsed


def test_health_responds_while_layout_is_being_read(live):
    client, reader, _ = live
    f = conftest.Family(client)
    elapsed = _health_while(
        client,
        reader,
        lambda: client.post(
            "/api/v1/pillboxes/detect",
            files={"photo": ("box.png", SAMPLE_PNG, "image/png")},
            headers=f.g,
        ),
    )
    assert elapsed < READ_SECONDS / 2, f"/health 가 판독이 끝날 때까지 기다림 ({elapsed:.2f}초)"


def test_health_responds_while_refill_photo_is_being_read(live):
    client, reader, _ = live
    f = conftest.Family(client)
    f.register_pillbox()
    elapsed = _health_while(
        client,
        reader,
        lambda: client.post(
            "/api/v1/refills",
            files={"photo": ("box.png", SAMPLE_PNG, "image/png")},
            headers=f.g,
        ),
    )
    assert elapsed < READ_SECONDS / 2, f"/health 가 판독이 끝날 때까지 기다림 ({elapsed:.2f}초)"


def test_health_responds_while_dose_photo_is_being_read(live):
    client, reader, app = live
    f = conftest.Family(client)
    f.register_pillbox()
    f.put_schedule(week_schedule())
    conftest.clock.set(conftest.T0.replace(hour=8, minute=1))
    run_once(app)  # 복약 건을 여는 것은 1분 예약 작업이다
    events = client.get("/api/v1/dose-events/today", headers=f.e).json()["items"]
    event_id = next(e["id"] for e in events if e["state"] == "asked")
    elapsed = _health_while(
        client,
        reader,
        lambda: client.post(
            f"/api/v1/dose-events/{event_id}/photo",
            files={"photo": ("box.png", SAMPLE_PNG, "image/png")},
            headers=f.e,
        ),
    )
    assert elapsed < READ_SECONDS / 2, f"/health 가 판독이 끝날 때까지 기다림 ({elapsed:.2f}초)"
