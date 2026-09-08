"""한도 값.

숫자를 코드 여기저기에 흩어 두면 나중에 무엇이 걸려 멈췄는지 찾기 어렵다.
한곳에 모아 두고 종료 이유에 이름을 함께 남긴다. (INTERFACES.md 참고)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """작업 하나에 적용하는 한도."""

    # 도구 요청을 몇 번까지 받아 줄지. 넘으면 tool_limit 으로 종료한다.
    max_tool_calls: int = 20

    # 작업 전체 시간. 로컬 CPU 추론이 한 번에 20~30초 걸려서
    # 강의 예시(300초)보다 늘려 잡았다.
    task_timeout_seconds: float = 600.0

    # 도구 하나가 도는 시간.
    tool_timeout_seconds: float = 30.0

    # 도구 결과가 이보다 길면 자르고 잘렸다는 사실을 남긴다.
    # 자르지 않으면 대화가 부풀어 모델이 앞 내용을 잃는다.
    max_tool_output_chars: int = 16_000


DEFAULT_LIMITS = Limits()
