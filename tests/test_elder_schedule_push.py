"""복약 시각이 바뀌면 어르신 폰에 데이터 전용 푸시를 보낸다.

어르신 폰의 복약 알람은 폰이 스스로 예약하는 로컬 알람이라, 보호자가 시각을 저장한 뒤 어르신 앱이
한 번도 열리지 않으면 알람이 울리지 않았다 (2026-11-14 시연 리허설에서 확인).
"""

from app.push import PushMessage
from tests.conftest import Family, week_schedule
from tests.test_fcm_push import FakeFcm, make_sender, rsa_key, sa_file  # noqa: F401 (fixtures)


def test_put_schedule_sends_silent_push_to_elder_only(client, push):
    f = Family(client)
    client.put("/api/v1/devices/me/push-token", json={"push_token": "elder-tok"}, headers=f.e)
    client.put("/api/v1/devices/me/push-token", json={"push_token": "guardian-tok"}, headers=f.g)
    f.register_pillbox()
    push.sent.clear()

    f.put_schedule(week_schedule())

    silent = [m for m in push.sent if m.silent]
    assert [m.push_token for m in silent] == ["elder-tok"]
    assert silent[0].data == {"type": "schedule_changed"}
    assert silent[0].title == "" and silent[0].body == ""
    # 보호자에게는 이 일로 푸시를 보내지 않는다 (원칙 2: 알림을 늘리지 않는다)
    assert all(m.push_token != "guardian-tok" for m in push.sent)


def test_rejected_schedule_sends_nothing(client, push):
    f = Family(client)
    client.put("/api/v1/devices/me/push-token", json={"push_token": "elder-tok"}, headers=f.e)
    f.register_pillbox()
    push.sent.clear()

    bad = [{"weekday": 0, "time_of_day": "morning", "dose_time": "08:00", "cell_id": "없는칸"}]
    res = client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g)

    assert res.status_code == 422
    assert push.sent == []


def test_fcm_silent_payload_has_no_notification(sa_file):  # noqa: F811
    fake = FakeFcm()
    sender = make_sender(sa_file, fake)
    sender.send([PushMessage("d1", "tok", "", "", {"type": "schedule_changed"}, silent=True)])

    (_, body) = fake.sends[0]
    assert body == {
        "validate_only": False,
        "message": {
            "token": "tok",
            "data": {"type": "schedule_changed"},
            "android": {"priority": "HIGH"},
        },
    }
