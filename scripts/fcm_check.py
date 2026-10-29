"""FCM 인증 확인 (dry-run). 실제로 보내지 않는다 (validate_only).

  uv run python scripts/fcm_check.py <서비스 계정 JSON 경로> [기기 FCM 토큰]

토큰을 주지 않으면 가짜 토큰으로 보낸다. 인증이 되면 FCM 이 "토큰 형식이 틀림"(400)으로 답하고,
인증이 안 되면 401/403 이 온다. 키·액세스 토큰 내용은 출력하지 않는다.
"""

import sys

from app.push import FcmPushSender, PushMessage, _bad_token_field, _fcm_error_code


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sender = FcmPushSender(sys.argv[1], validate_only=True)
    sender.access_token()
    print("oauth: ok (access token issued)")
    token = sys.argv[2] if len(sys.argv) > 2 else "dry-run-invalid-token"
    res = sender._post(PushMessage("check", token, "확인", "dry-run", {"type": "check"}))
    if res.status_code == 200:
        print("fcm validate_only: HTTP 200 OK (메시지와 토큰 모두 유효)")
        return 0
    code = _fcm_error_code(res)
    print(f"fcm validate_only: HTTP {res.status_code} {code} bad_token={_bad_token_field(res)}")
    if res.status_code in (401, 403):
        print("인증 실패: 서비스 계정 권한 또는 Firebase Cloud Messaging API 사용 설정을 확인")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
