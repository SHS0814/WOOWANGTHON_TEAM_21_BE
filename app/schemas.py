"""여러 라우터가 같이 쓰는 입력 형식."""

from typing import Annotated

from pydantic import StringConstraints

# 호칭·이름: 앞뒤 공백을 지운 뒤 1~20자 (api.md 3.2, R-021 Q-002)
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
