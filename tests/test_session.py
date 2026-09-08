"""세션 저장·복원 검증. (ACCEPTANCE A09 / R06 / D07)

이 파일은 A08 을 실제로 돌려 보다 터진 버그 때문에 생겼다.
저장할 때 dataclass 를 펴 놓고 복원할 때 되돌리지 않아,
불러온 대화를 provider 로 넘기는 곳에서 'dict' object has no attribute 'id' 가 났다.

한 번 실행 안에서 끝나는 시험만 있으면 파일을 거쳐 돌아오는 경로를 아무도 지나가지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.providers import OllamaProvider, ToolRequest
from harness.session import Recorder, SessionStore, mask_secrets


def sample_conversation() -> list[dict]:
    return [
        {"role": "user", "content": "posts/ok.md 읽어줘"},
        {
            "role": "assistant",
            "content": "",
            "tool_requests": [ToolRequest("call_1", "read_file", {"path": "posts/ok.md"})],
        },
        {"role": "tool", "tool_name": "read_file", "content": '{"ok": true, "text": "---"}'},
        {"role": "assistant", "content": "제목은 테스트입니다."},
    ]


def test_a09_missing_session_returns_none(tmp_path: Path) -> None:
    """기록이 없으면 None 이다. 빈 목록을 돌려주며 복원했다고 하면 안 된다."""
    store = SessionStore(tmp_path / "sessions")
    assert store.load("있지도-않은-세션") is None
    assert store.exists("있지도-않은-세션") is False


def test_a09_round_trip_keeps_tool_request_type(tmp_path: Path) -> None:
    """저장했다 불러오면 ToolRequest 가 그대로 ToolRequest 여야 한다."""
    store = SessionStore(tmp_path / "sessions")
    session_id = store.new_id()
    store.save(session_id, sample_conversation())

    restored = store.load(session_id)
    assert restored is not None

    requests = restored[1]["tool_requests"]
    assert isinstance(requests[0], ToolRequest)
    assert requests[0].id == "call_1"
    assert requests[0].name == "read_file"
    assert requests[0].arguments == {"path": "posts/ok.md"}


def test_a09_restored_conversation_can_be_sent_to_provider(tmp_path: Path) -> None:
    """복원한 대화가 실제로 provider 형식으로 바뀌는가.

    A08 실행에서 터진 바로 그 지점이다. 여기서 막아 둔다.
    """
    store = SessionStore(tmp_path / "sessions")
    session_id = store.new_id()
    store.save(session_id, sample_conversation())
    restored = store.load(session_id)

    provider = OllamaProvider()
    wire = provider._to_wire(restored)  # 터진 지점이라 일부러 직접 부른다

    assert wire[1]["tool_calls"][0]["id"] == "call_1"
    assert wire[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire[2]["role"] == "tool"
    assert wire[2]["tool_name"] == "read_file"


def test_a09_saved_file_has_no_tunnel_address(tmp_path: Path) -> None:
    """세션 파일에 바깥 주소가 남지 않는다. (R06 민감정보 제외)"""
    store = SessionStore(tmp_path / "sessions")
    session_id = store.new_id()
    store.save(
        session_id,
        [{"role": "user", "content": "https://abc-123.trycloudflare.com 에 연결해봐"}],
    )
    saved = store.conversation_path(session_id).read_text(encoding="utf-8")
    assert "trycloudflare.com" not in saved


def test_session_list_shows_saved_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.new_id()
    store.save(first, sample_conversation())
    assert first in store.list_ids()


def test_recorder_write_accepts_a_kind_field(tmp_path: Path) -> None:
    """기록 필드에 'kind' 라는 이름을 써도 터지지 않아야 한다.

    Recorder.write 의 첫 인자와 이름이 겹쳐 터진 적이 있다.
    """
    recorder = Recorder(tmp_path / "events.jsonl")
    recorder.write("provider_error", error_kind="connection", message="연결 실패")

    line = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()
    event = json.loads(line)
    assert event["kind"] == "provider_error"
    assert event["error_kind"] == "connection"


def test_mask_keeps_localhost() -> None:
    assert "localhost" in mask_secrets("http://localhost:11434/api/chat 로 요청")
