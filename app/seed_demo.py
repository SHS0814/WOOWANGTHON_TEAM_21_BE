"""시연·심사용 데모 가족 만들기 (R-010 10번, R-007 C4, R-015).

실행 중인 서버의 API를 그대로 불러서 만든다 (서버에 DEMO_MODE=true 필요).
데모 가족, 보호자·어르신 기기, 약통, 일정, 모두 채운 기준선을 한 번에 만든다.
약통은 2×7 일주일 약통(기본) 또는 `--pillbox day` 1×3 하루 약통(아침·점심·저녁, R-054).
하루 약통은 같은 칸을 매일 쓰므로 저녁 복약 뒤 리필(POST /refills)해야 다음 날 판독된다.

    docker compose exec api uv run --no-sync python -m app.seed_demo create
    uv run python -m app.seed_demo create --base-url http://127.0.0.1:8000

심사 종료(11/21) 뒤 데모 가족의 모든 기기 토큰 폐기 (DB 직접, 마지막 보호자 포함):

    docker compose exec api uv run --no-sync python -m app.seed_demo revoke

토큰과 초대 코드는 **화면에만** 출력한다. 파일로 저장하거나 커밋하지 않는다.
심사 접근 정보는 비공개 제출 폼에만 적고, 심사가 끝나면(11/21) 기기를 해제한다.
"""

import argparse
import sys
from pathlib import Path

import httpx

from harness.make_samples import draw

WEEK = ["월", "화", "수", "목", "금", "토", "일"]
TIMES = {"morning": ("아침", "08:00"), "evening": ("저녁", "19:00")}


def full_pillbox_png(rows: int = 2, cols: int = 7) -> bytes:
    """모든 칸에 약이 있는 합성 약통 이미지 (실제 사진 아님)."""
    return draw(rows, cols, {f"r{r}c{c}": "full" for r in range(rows) for c in range(cols)})


def _ok(res: httpx.Response, status: int = 200) -> dict:
    if res.status_code != status:
        raise SystemExit(
            f"{res.request.method} {res.request.url.path} → {res.status_code}: {res.text}"
        )
    return res.json() if res.content else {}


def seed(
    client: httpx.Client,
    *,
    elder_name: str = "어머니",
    guardian_name: str = "민수",
    morning: str = "08:00",
    evening: str = "19:00",
    noon: str = "12:00",
    pillbox: str = "week",
    photo: bytes | None = None,
) -> dict:
    if pillbox not in ("week", "day"):
        raise SystemExit(f"pillbox 는 week 또는 day: {pillbox}")
    daily = pillbox == "day"
    api = "/api/v1"
    fam = _ok(
        client.post(
            f"{api}/families",
            json={
                "elder_display_name": elder_name,
                "guardian_display_name": guardian_name,
                "is_demo": True,
            },
        ),
        201,
    )
    g = {"Authorization": f"Bearer {fam['token']}"}

    code = _ok(client.post(f"{api}/pairing-codes", json={"kind": "elder"}, headers=g), 201)
    elder = _ok(client.post(f"{api}/pair", json={"code": code["code"]}), 201)
    e = {"Authorization": f"Bearer {elder['token']}"}
    me = _ok(client.get(f"{api}/me", headers=e))
    _ok(
        client.post(
            f"{api}/consent",
            json={
                "agreed": True,
                "consent_text_version": me["server"]["consent_text_version"],
                "ai_external_transfer_shown": me["server"]["ai_external_transfer"],
            },
            headers=e,
        )
    )

    rows, cols = (1, 3) if daily else (2, 7)
    image = photo or full_pillbox_png(rows, cols)
    det = _ok(
        client.post(
            f"{api}/pillboxes/detect",
            files={"photo": ("pillbox.png", image, "image/png")},
            headers=g,
        )
    )
    if daily:
        layout = {"rows": 1, "cols": 3, "row_labels": [], "col_labels": ["아침", "점심", "저녁"]}
    else:
        layout = {"rows": 2, "cols": 7, "row_labels": ["아침", "저녁"], "col_labels": WEEK}
    pb = _ok(
        client.post(
            f"{api}/pillboxes",
            json={"photo_id": det["photo"]["id"], "layout": layout},
            headers=g,
        ),
        201,
    )
    # 판독기와 관계없이 기준선은 "모두 채움"으로 맞춘다 (보호자가 앱에서 확인하는 단계와 같음)
    refill = pb["refill"]
    _ok(
        client.put(
            f"{api}/refills/{refill['id']}/baseline",
            json={
                "cells": [
                    {"cell_id": c["cell_id"], "state": "full"} for c in refill["baseline_cells"]
                ]
            },
            headers=g,
        )
    )
    if daily:
        # 같은 칸을 매일 쓴다 (R-054)
        times = (("morning", morning), ("noon", noon), ("evening", evening))
        slots = [
            {"weekday": wd, "time_of_day": tod, "dose_time": t, "cell_id": f"r0c{col}"}
            for wd in range(7)
            for col, (tod, t) in enumerate(times)
        ]
    else:
        slots = [
            {"weekday": wd, "time_of_day": tod, "dose_time": t, "cell_id": f"r{row}c{wd}"}
            for row, (tod, t) in enumerate((("morning", morning), ("evening", evening)))
            for wd in range(7)
        ]
    _ok(client.put(f"{api}/schedule", json={"slots": slots}, headers=g))
    invite = _ok(client.post(f"{api}/pairing-codes", json={"kind": "guardian"}, headers=g), 201)
    return {
        "family_id": fam["family_id"],
        "guardian_token": fam["token"],
        "elder_token": elder["token"],
        "guardian_invite_code": invite["code"],
        "guardian_invite_expires_at": invite["expires_at"],
        "demo_mode": me["server"]["demo_mode"],
        "reader_is_mock": me["server"]["reader_is_mock"],
    }


def revoke_demo_devices(database_url: str) -> int:
    """데모 가족(is_demo)의 모든 기기 토큰을 폐기한다. 폐기한 기기 수를 돌려준다."""
    from sqlalchemy import select

    from app.clock import clock
    from app.db import make_engine, make_sessionmaker
    from app.models import Device, Family

    sessionmaker = make_sessionmaker(make_engine(database_url))
    with sessionmaker() as db:
        devices = db.scalars(
            select(Device)
            .join(Family, Family.id == Device.family_id)
            .where(Family.is_demo, Device.revoked_at.is_(None))
        ).all()
        for d in devices:
            d.revoked_at = clock.now()
        db.commit()
        return len(devices)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="데모 가족 만들기 (서버 API 사용, DEMO_MODE=true 필요)")
    c.add_argument("--base-url", default="http://127.0.0.1:8000")
    c.add_argument("--elder-name", default="어머니")
    c.add_argument("--guardian-name", default="민수")
    c.add_argument("--morning", default="08:00")
    c.add_argument("--evening", default="19:00")
    c.add_argument("--noon", default="12:00", help="하루 약통의 점심 시각")
    c.add_argument(
        "--pillbox",
        choices=["week", "day"],
        default="week",
        help="week: 2×7 일주일 약통, day: 1×3 하루 약통 (아침·점심·저녁, 저녁 뒤 리필)",
    )
    c.add_argument("--photo", help="채운 약통 사진 경로 (없으면 합성 이미지)")
    sub.add_parser("revoke", help="데모 가족의 모든 기기 토큰 폐기 (DATABASE_URL 사용)")
    args = ap.parse_args(argv)

    if args.cmd == "revoke":
        from app.config import get_settings

        n = revoke_demo_devices(get_settings().database_url)
        print(f"데모 가족 기기 {n}대의 토큰을 폐기했다.")
        return 0

    photo = Path(args.photo).read_bytes() if args.photo else None
    with httpx.Client(base_url=args.base_url, timeout=90) as client:
        out = seed(
            client,
            elder_name=args.elder_name,
            guardian_name=args.guardian_name,
            morning=args.morning,
            evening=args.evening,
            noon=args.noon,
            pillbox=args.pillbox,
            photo=photo,
        )
    print(
        "\n".join(
            [
                "데모 가족을 만들었다. 아래 값은 이 화면에만 있다. 파일로 저장하거나 커밋하지 마라.",
                "심사 접근 정보는 비공개 제출 폼에만 적는다 (R-007 C4).",
                "",
                f"family_id            {out['family_id']}",
                f"보호자 토큰           {out['guardian_token']}",
                f"어르신 토큰           {out['elder_token']}",
                (
                    f"보호자 초대 코드       {out['guardian_invite_code']}"
                    f"  (만료 {out['guardian_invite_expires_at']})"
                ),
                f"모의 판독             {'예 (앱에 모의 판독 표시)' if out['reader_is_mock'] else '아니요'}",
                "",
                '시연 트리거: POST /api/v1/dev/dose-events {"slot_id": ...} (오늘 요일 칸)',
                "심사 종료(11/21) 뒤: python -m app.seed_demo revoke 로 토큰 폐기 (docs/deploy.md 7장)",
            ]
        ),
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
