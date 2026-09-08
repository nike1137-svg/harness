"""세션 저장과 실행 기록. (D07 / R06)

저장 위치는 work/ 밖이다. 작업 폴더 안에 두면 하네스가 자기 기록을
도구로 읽고 쓸 수 있게 된다.

기록은 한 줄에 한 사건씩 덧붙인다. 중간에 프로그램이 죽어도
그때까지 쓴 줄은 남는다.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .providers import ToolRequest

# 기록에 새면 안 되는 것들. 터널 주소와 키가 오류 메시지에 섞여 들어올 수 있다.
_SECRET_PATTERNS = [
    re.compile(r"https?://(?!localhost|127\.0\.0\.1)[^\s\"']+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|authtoken|bearer)\s*[:=]\s*\S+"),
]


def mask_secrets(text: str) -> str:
    """바깥 주소와 키로 보이는 것을 가린다.

    완벽한 차단이 아니라 실수로 새는 것을 줄이는 장치다.
    localhost 는 남긴다. 개발 중에 그것까지 가리면 기록이 쓸모없어진다.
    """
    masked = text
    for pattern in _SECRET_PATTERNS:
        masked = pattern.sub("[가림]", masked)
    return masked


def _plain(value: Any) -> Any:
    """dataclass 와 Path 를 JSON 으로 쓸 수 있는 형태로 바꾼다."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def describe(event: dict[str, Any]) -> str:
    """기록 한 줄을 사람이 읽을 문장으로 바꾼다.

    무엇을 부르는 중인지 보이지 않으면, 느린 모델이 도는 동안
    멈춘 것인지 일하는 중인지 알 수 없다.
    """
    kind = event.get("kind")

    if kind == "task_start":
        return f"[요청] {event.get('request', '')}"
    if kind == "tool_request":
        arguments = json.dumps(event.get("arguments", {}), ensure_ascii=False)
        return f"[도구] {event.get('name')} {arguments}"
    if kind == "tool_result":
        if event.get("ok"):
            return "        → 성공"
        code = event.get("code")
        if code:
            return f"        → 실패 ({code})"
        # run_command 가 0 아닌 종료 코드를 돌려준 경우다. 오류 코드는 없지만
        # 실패는 실패다. 종료 코드를 그대로 보여 준다.
        exit_code = (event.get("summary") or {}).get("exit_code")
        if exit_code is not None:
            return f"        → 실패 (종료 코드 {exit_code})"
        return "        → 실패"
    if kind == "approval_rejected":
        return f"        → 거절함 ({event.get('name')})"
    if kind == "provider_error":
        return f"[오류] {event.get('error_kind')}: {event.get('message')}"
    if kind == "tool_limit":
        return f"[중단] 도구 호출 한도에 걸렸습니다 ({event.get('used')}회)"
    if kind == "task_end":
        # 바뀐 파일 수를 함께 찍는다. completed 인데 0 건이면 모델이
        # 하겠다고만 하고 끝냈을 수 있다. 사용자가 그걸 알아야 한다.
        return (
            f"[종료] {event.get('state')} / 이유 {event.get('reason')} / "
            f"도구 {event.get('tool_calls_used')}회 / "
            f"파일 {event.get('files_changed', 0)}건 / {event.get('elapsed_seconds')}초"
        )
    return ""


class Recorder:
    """실행 기록을 파일에 남긴다."""

    def __init__(self, path: Path, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event_kind: str, /, **fields: Any) -> None:
        # event_kind 를 위치 인자로만 받는다(`/`). 이름으로도 받게 두면
        # write("provider_error", kind=...) 처럼 부를 때 인자가 겹쳐 터진다.
        event = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": event_kind,
            **{key: _plain(value) for key, value in fields.items()},
        }
        line = json.dumps(event, ensure_ascii=False)
        line = mask_secrets(line)

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

        if self.echo:
            described = describe(event)
            if described:
                print(described, flush=True)


def revive_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """저장하면서 딕셔너리가 된 ToolRequest 를 원래 형으로 되돌린다.

    저장할 때는 dataclass 를 펴서 JSON 으로 쓴다. 그대로 불러오면
    딕셔너리라서 `request.id` 같은 접근이 터진다.
    저장과 복원은 짝이다. 한쪽만 만들면 왕복에서 타입이 바뀐다.
    """
    revived: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        requests = item.get("tool_requests")
        if requests:
            item["tool_requests"] = [
                entry if isinstance(entry, ToolRequest) else ToolRequest(**entry)
                for entry in requests
            ]
        revived.append(item)
    return revived


class SessionStore:
    """세션 하나의 대화를 파일에 두고 다시 불러온다.

    복원할 기록이 없으면 없다고 말한다. 빈 대화를 돌려주면서
    '복원했다' 고 하지 않는다. (A09)
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    def conversation_path(self, session_id: str) -> Path:
        return self.root / session_id / "conversation.json"

    def events_path(self, session_id: str) -> Path:
        return self.root / session_id / "events.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.conversation_path(session_id).is_file()

    def load(self, session_id: str) -> list[dict[str, Any]] | None:
        """없으면 None. 빈 목록과 구별해야 한다."""
        path = self.conversation_path(session_id)
        if not path.is_file():
            return None
        return revive_messages(json.loads(path.read_text(encoding="utf-8")))

    def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        path = self.conversation_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_plain(messages), ensure_ascii=False, indent=2)
        path.write_text(mask_secrets(payload), encoding="utf-8")

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and (child / "conversation.json").is_file()
        )
