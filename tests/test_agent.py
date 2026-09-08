"""모의 모델로 확인하는 검증. (ACCEPTANCE A02·A05·A06·A07)

실제 모델을 쓰지 않는다. 하네스 쪽 흐름이 맞는지부터 가려내려는 것이다.
여기가 통과해야 '모델이 도구를 안 골랐다' 와 '내 코드가 틀렸다' 를 구별할 수 있다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent import Agent, TaskState
from harness.limits import Limits
from harness.providers import (
    FakeProvider,
    ModelReply,
    ProviderError,
    ToolRequest,
    normalize_arguments,
)
from harness.session import Recorder, mask_secrets
from harness.tools import ToolError, resolve_inside


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """작은 실습 폴더 하나."""
    root = tmp_path / "work"
    (root / "posts").mkdir(parents=True)
    (root / "posts" / "ok.md").write_text(
        "---\ntitle: 테스트 글\ndate: 2026-01-01 09:00:00 +0900\n---\n\n본문.\n",
        encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("작업 폴더 밖의 파일", encoding="utf-8")
    return root


def make_agent(work: Path, provider, **kwargs) -> Agent:
    recorder = Recorder(work.parent / "events.jsonl")
    return Agent(provider=provider, work_root=work, recorder=recorder, **kwargs)


def tool_messages(outcome) -> list[dict]:
    return [m for m in outcome.messages if m["role"] == "tool"]


def payload_of(message: dict) -> dict:
    return json.loads(message["content"])


# --------------------------------------------------------------- A02

def test_a02_tool_request_then_result_then_finish(work: Path) -> None:
    """도구 요청 → 인자 검사 → 실행 → 결과가 대화에 들어감 → 종료."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("call_1", "read_file", {"path": "posts/ok.md"})]),
            ModelReply(text="제목은 '테스트 글' 입니다."),
        ]
    )
    outcome = make_agent(work, provider).run("posts/ok.md 의 제목을 알려줘")

    assert outcome.state is TaskState.COMPLETED
    assert outcome.reason == "final_answer"
    assert outcome.tool_calls_used == 1

    # 도구 결과가 실제로 대화에 들어갔는지 — 이걸 빠뜨리면 모델이 같은 도구를 반복한다.
    results = tool_messages(outcome)
    assert len(results) == 1
    assert "테스트 글" in payload_of(results[0])["text"]

    # 두 번째 모델 호출이 도구 결과를 보고 있었는지 확인한다.
    second_call = provider.calls[1]
    assert second_call[-1]["role"] == "tool"


def test_a02_assistant_tool_request_stays_in_conversation(work: Path) -> None:
    """도구를 부르기로 한 응답도 대화에 남아야 한다."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("call_1", "read_file", {"path": "posts/ok.md"})]),
            ModelReply(text="끝"),
        ]
    )
    outcome = make_agent(work, provider).run("읽어줘")
    assistant_with_tools = [
        m for m in outcome.messages if m["role"] == "assistant" and m.get("tool_requests")
    ]
    assert len(assistant_with_tools) == 1


# --------------------------------------------------------------- A05

@pytest.mark.parametrize(
    "bad_path",
    ["../secret.txt", "posts/../../secret.txt", "C:/Windows/System32/drivers/etc/hosts"],
)
def test_a05_path_outside_work_is_refused(work: Path, bad_path: str) -> None:
    """모델이 요청해도 코드가 막는다."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "read_file", {"path": bad_path})]),
            ModelReply(text="읽지 못했습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("그 파일 읽어줘")

    result = payload_of(tool_messages(outcome)[0])
    assert result["ok"] is False
    assert result["code"] == "PATH_OUTSIDE_WORK"


def test_a05_resolve_inside_accepts_normal_path(work: Path) -> None:
    assert resolve_inside(work, "posts/ok.md").is_file()


def test_a05_resolve_inside_rejects_non_string(work: Path) -> None:
    with pytest.raises(ToolError) as caught:
        resolve_inside(work, 123)
    assert caught.value.code == "BAD_ARGUMENTS"


# --------------------------------------------------------------- A06

def test_a06_tool_limit_stops_the_loop(work: Path) -> None:
    """도구를 계속 부르는 모델이 한도에서 끊기는가."""
    provider = FakeProvider(
        [ModelReply(tool_requests=[ToolRequest("c", "read_file", {"path": "posts/ok.md"})])],
        repeat_last=True,
    )
    agent = make_agent(work, provider, limits=Limits(max_tool_calls=3))
    outcome = agent.run("계속 읽어줘")

    assert outcome.state is TaskState.FAILED
    assert outcome.reason == "tool_limit"
    assert outcome.tool_calls_used == 3


# --------------------------------------------------------------- A07

def test_a07_bad_arguments_do_not_run_the_tool(work: Path) -> None:
    """잘못된 인자는 실행하지 않고 오류를 모델에게 돌려준다."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "read_file", {"path": 123})]),
            ModelReply(text="인자가 잘못됐습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("읽어줘")

    result = payload_of(tool_messages(outcome)[0])
    assert result["ok"] is False
    assert result["code"] == "BAD_ARGUMENTS"
    # 오류가 나도 루프는 계속되고 최종 답까지 간다.
    assert outcome.state is TaskState.COMPLETED


def test_a07_unknown_tool_is_refused(work: Path) -> None:
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "delete_everything", {})]),
            ModelReply(text="그런 도구가 없습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("지워줘")
    assert payload_of(tool_messages(outcome)[0])["code"] == "UNKNOWN_TOOL"


def test_a07_missing_file_is_reported(work: Path) -> None:
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "read_file", {"path": "posts/none.md"})]),
            ModelReply(text="없습니다."),
        ]
    )
    outcome = make_agent(work, provider).run("읽어줘")
    assert payload_of(tool_messages(outcome)[0])["code"] == "FILE_NOT_FOUND"


def test_a07_provider_failure_is_not_success(work: Path) -> None:
    """모델에 연결하지 못하면 실패다. 성공으로 기록하지 않는다."""

    class BrokenProvider:
        name = "broken"

        def complete(self, messages, tool_definitions):
            raise ProviderError("connection", "서버에 연결하지 못했습니다.")

    outcome = make_agent(work, BrokenProvider()).run("읽어줘")
    assert outcome.state is TaskState.FAILED
    assert outcome.reason == "provider_connection"
    assert outcome.succeeded is False


def test_empty_request_is_refused(work: Path) -> None:
    outcome = make_agent(work, FakeProvider([])).run("   ")
    assert outcome.reason == "empty_request"


# ------------------------------------------------- provider 어댑터 (R07)

def test_arguments_from_ollama_are_a_dict() -> None:
    """Ollama 는 파싱된 객체를 준다."""
    assert normalize_arguments({"path": "a.md"}, "read_file") == {"path": "a.md"}


def test_arguments_from_openai_style_are_a_json_string() -> None:
    """OpenAI 호환 API 는 JSON 문자열을 준다. 어댑터가 흡수한다."""
    assert normalize_arguments('{"path": "a.md"}', "read_file") == {"path": "a.md"}


def test_broken_arguments_raise_protocol_error() -> None:
    with pytest.raises(ProviderError) as caught:
        normalize_arguments("{not json", "read_file")
    assert caught.value.kind == "protocol"


# -------------------------------------------------------- 기록 (R06/D07)

def test_secrets_are_masked_in_records() -> None:
    """터널 주소가 기록에 그대로 남으면 안 된다."""
    masked = mask_secrets("연결 실패: https://abc-123.trycloudflare.com/v1/chat")
    assert "trycloudflare.com" not in masked
    # 로컬 주소는 남긴다. 개발 중 기록이 쓸모없어지면 안 된다.
    assert "localhost" in mask_secrets("http://localhost:11434 에 연결")


def test_events_are_written(work: Path) -> None:
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "read_file", {"path": "posts/ok.md"})]),
            ModelReply(text="끝"),
        ]
    )
    agent = make_agent(work, provider)
    agent.run("읽어줘")

    lines = (work.parent / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "task_start" in kinds
    assert "tool_request" in kinds
    assert "tool_result" in kinds
    assert "task_end" in kinds
