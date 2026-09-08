"""harness-lab 의 --agent 옵션이 가리키는 껍데기.

실제 구현은 제출할 하네스 저장소의 `src/harness/benchmark.py` 에 있다.
여기에 코드를 두지 않는 이유: 이 폴더는 강의가 배포한 평가 자료이고,
제출물은 내 저장소에 있어야 한다. 이 파일은 import 경로만 이어 준다.

쓰기 전에 하네스 저장소의 src 경로를 환경 변수로 알려 준다.

    # PowerShell
    $env:MY_HARNESS_SRC = "C:\어딘가\harness\src"

    # bash
    export MY_HARNESS_SRC=/어딘가/harness/src

기본값을 두지 않는 이유는, 내 컴퓨터 경로를 코드에 박아 두면
다른 사람이 받았을 때 그대로 실패하기 때문이다.
"""

import os
import sys
from pathlib import Path

raw = os.environ.get("MY_HARNESS_SRC")
if not raw:
    raise RuntimeError(
        "환경 변수 MY_HARNESS_SRC 에 하네스 저장소의 src 경로를 넣으세요.\n"
        "  예: MY_HARNESS_SRC=/path/to/harness/src"
    )

HARNESS_SRC = Path(raw)
if not (HARNESS_SRC / "harness" / "benchmark.py").is_file():
    raise RuntimeError(f"하네스 소스를 찾지 못했습니다: {HARNESS_SRC}")

if str(HARNESS_SRC) not in sys.path:
    sys.path.insert(0, str(HARNESS_SRC))

from harness.benchmark import solve_task  # noqa: E402,F401

__all__ = ["solve_task"]
