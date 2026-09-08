"""성공을 가장한 실패를 만들지 않는지 본다. (R05 / A07)

2026-09-08 실제 실행에서 드러난 세 가지를 고정한다.

1. run_command 가 ok=False 를 돌려줘도 기록·화면에 "성공" 으로 찍혔다.
   agent 가 recorder 에 ok=True 를 못박아 넘기고 있었다.
2. 모델이 "수정하겠습니다" 라고 쓰고 도구를 부르지 않은 채 대화를 끝냈는데
   상태가 completed 였다. 파일을 열어보지 않으면 속는다.
3. 모델에게 주는 지침이 아예 없었다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent import SYSTEM_PROMPT, WRITE_TOOLS, Agent, TaskState
from harness.providers import FakeProvider, ModelReply, ToolRequest
from harness.session import Recorder


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "checker.py").write_text("원래 내용\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    return root


def make_agent(work: Path, provider, **kwargs) -> Agent:
    return Agent(
        provider=provider,
        work_root=work,
        recorder=Recorder(work.parent / "events.jsonl"),
        **kwargs,
    )


def events_of(work: Path) -> list[dict]:
    path = work.parent / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def approve_all(request: ToolRequest) -> bool:
    return True


# ------------------------------------------------- 1. 실패를 성공으로 찍지 않기

def test_failing_command_is_recorded_as_not_ok(work: Path) -> None:
    """pytest 가 실패하면 기록에도 ok=False 로 남는다."""
    (work / "tests" / "test_fail.py").write_text(
        "def test_x():\n    assert False\n", encoding="utf-8"
    )

    provider = FakeProvider(
        [
            ModelReply(
                tool_requests=[
                    ToolRequest("c1", "run_command", {"argv": ["pytest", "tests/", "-q"]})
                ]
            ),
            ModelReply(text="테스트가 실패했습니다."),
        ]
    )
    make_agent(work, provider, approver=approve_all).run("테스트 돌려줘")

    results = [event for event in events_of(work) if event.get("kind") == "tool_result"]
    assert results, "tool_result 기록이 없다"
    assert results[-1]["ok"] is False


def test_passing_command_is_recorded_as_ok(work: Path) -> None:
    """대조군. 통과하면 ok=True 다."""
    (work / "tests" / "test_pass.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )

    provider = FakeProvider(
        [
            ModelReply(
                tool_requests=[
                    ToolRequest("c1", "run_command", {"argv": ["pytest", "tests/", "-q"]})
                ]
            ),
            ModelReply(text="통과했습니다."),
        ]
    )
    make_agent(work, provider, approver=approve_all).run("테스트 돌려줘")

    results = [event for event in events_of(work) if event.get("kind") == "tool_result"]
    assert results[-1]["ok"] is True


# ------------------------------------------------- 2. 바뀐 파일 수를 세기

def test_talking_without_editing_reports_zero_files_changed(work: Path) -> None:
    """모델이 하겠다고만 하고 끝내면 files_changed 가 0 이다.

    실제로 4B 모델이 "위와 같이 내용을 수정하겠습니다" 라고 쓰고 대화를
    끝냈다. 상태는 completed 였고 파일은 그대로였다.
    """
    provider = FakeProvider([ModelReply(text="위와 같이 내용을 수정하겠습니다.")])
    outcome = make_agent(work, provider, approver=approve_all).run("checker.py 고쳐줘")

    assert outcome.state is TaskState.COMPLETED   # 모델은 말을 마쳤다
    assert outcome.files_changed == 0             # 그러나 바뀐 것은 없다


def test_successful_edit_counts_one_file(work: Path) -> None:
    provider = FakeProvider(
        [
            ModelReply(
                tool_requests=[
                    ToolRequest(
                        "c1",
                        "edit_file",
                        {"path": "checker.py", "old_string": "원래", "new_string": "고친"},
                    )
                ]
            ),
            ModelReply(text="고쳤습니다."),
        ]
    )
    outcome = make_agent(work, provider, approver=approve_all).run("고쳐줘")

    assert outcome.files_changed == 1
    assert (work / "checker.py").read_text(encoding="utf-8") == "고친 내용\n"


def test_rejected_edit_counts_zero(work: Path) -> None:
    """거절된 편집은 세지 않는다."""
    provider = FakeProvider(
        [
            ModelReply(
                tool_requests=[
                    ToolRequest(
                        "c1",
                        "edit_file",
                        {"path": "checker.py", "old_string": "원래", "new_string": "고친"},
                    )
                ]
            ),
            ModelReply(text="거절되었습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("고쳐줘")   # 기본값은 거절
    assert outcome.files_changed == 0


def test_read_only_task_counts_zero(work: Path) -> None:
    """읽기만 한 작업도 0 이다. 그것은 정상이며 경고 대상이 아니다."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "read_file", {"path": "checker.py"})]),
            ModelReply(text="읽었습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("읽어줘")

    assert outcome.files_changed == 0
    # 쓰기 도구를 부르려 한 적이 없다. cli 는 이것으로 경고 여부를 가른다.
    tried = any(
        request.name in WRITE_TOOLS
        for message in outcome.messages
        if message.get("role") == "assistant"
        for request in (message.get("tool_requests") or [])
    )
    assert tried is False


# ------------------------------------------------- 3. 지침을 실제로 보내기

def test_system_prompt_is_sent_to_the_model(work: Path) -> None:
    provider = FakeProvider([ModelReply(text="네")])
    outcome = make_agent(work, provider).run("안녕")

    system_messages = [m for m in outcome.messages if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == SYSTEM_PROMPT


def test_system_prompt_is_not_duplicated_when_resuming(work: Path) -> None:
    """세션을 이어갈 때 지침이 두 번 들어가지 않는다."""
    provider = FakeProvider([ModelReply(text="첫 답")])
    first = make_agent(work, provider).run("첫 요청")

    provider2 = FakeProvider([ModelReply(text="둘째 답")])
    second = make_agent(work, provider2).run("둘째 요청", messages=first.messages)

    assert len([m for m in second.messages if m["role"] == "system"]) == 1


def test_system_prompt_tells_model_not_to_just_talk(work: Path) -> None:
    """지침에 핵심 문구가 들어 있는지 고정한다."""
    assert "말만 하고 끝내지 마라" in SYSTEM_PROMPT
    assert "역슬래시" in SYSTEM_PROMPT      # 이스케이프 실수 방지
    assert "edit_file" in SYSTEM_PROMPT
