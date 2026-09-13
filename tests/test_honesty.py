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
import time
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


# ------------------------------------------------- 4. 예산 안에서 끝내기

def test_model_call_is_capped_by_remaining_budget(work: Path) -> None:
    """모델 호출 하나가 남은 예산을 넘기지 않도록 타임아웃이 좁혀진다.

    2026-09-08 벤치마크: 한도 검사는 호출이 끝난 뒤에만 하므로, 예산이
    얼마 안 남았을 때 시작한 호출이 예산을 크게 넘겼다. 그 사이 평가
    도구가 먼저 끊어 이 하네스의 종료 이유와 사용량이 버려졌다.
    """
    from harness.limits import Limits

    class SlowProvider(FakeProvider):
        name = "slow"
        timeout_seconds = 999.0

        def complete(self, messages, tool_definitions):
            time.sleep(0.15)
            return super().complete(messages, tool_definitions)

    provider = SlowProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "list_files", {"path": "."})]),
            ModelReply(tool_requests=[ToolRequest("c2", "list_files", {"path": "."})]),
            ModelReply(text="끝"),
        ]
    )
    agent = make_agent(work, provider, limits=Limits(task_timeout_seconds=2.0))
    agent.run("살펴봐")

    # 남은 예산에 맞춰 좁혀졌다. 처음 값 999초를 그대로 쓰지 않는다.
    assert provider.timeout_seconds < 999.0
    assert provider.timeout_seconds >= 15.0      # 바닥값은 지킨다


# ------------------------------------------------- 5. 모델 서버 오류 재시도
#
# 2026-09-08 벤치마크 기준 측정: 10문항 중 5문항이 모델 호출 오류 한 번에
# 중단됐고, 그 시점에 도구를 2~8회밖에 쓰지 않았다(한도 160회).
# 개선 실험은 max_provider_retries 만 올려서 같은 조건으로 재측정한다.


class FlakyProvider(FakeProvider):
    """앞의 n번은 실패하고 그 다음부터 정상 응답하는 제공자."""

    name = "flaky"
    timeout_seconds = 300.0

    def __init__(
        self,
        replies,
        fail_times: int,
        kind: str = "connection",
        status: int | None = None,
    ) -> None:
        super().__init__(replies)
        self.fail_times = fail_times
        self.kind = kind
        self.status = status
        self.call_count = 0

    def complete(self, messages, tool_definitions):
        from harness.providers import ProviderError

        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ProviderError(self.kind, "일시적인 오류", status=self.status)
        return super().complete(messages, tool_definitions)


def test_without_retry_one_error_ends_the_task(work: Path) -> None:
    """기준 동작: 한 번 실패하면 끝난다. 기본값은 재시도 0회다."""
    from harness.limits import Limits

    provider = FlakyProvider([ModelReply(text="답")], fail_times=1)
    outcome = make_agent(work, provider, limits=Limits()).run("해줘")

    assert outcome.state is TaskState.FAILED
    assert outcome.reason == "provider_connection"
    assert provider.call_count == 1


def test_with_retry_the_task_recovers(work: Path) -> None:
    """개선 동작: 재시도를 허용하면 일시적 오류를 넘어가 완주한다."""
    from harness.limits import Limits

    provider = FlakyProvider([ModelReply(text="답")], fail_times=1)
    limits = Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    outcome = make_agent(work, provider, limits=limits).run("해줘")

    assert outcome.state is TaskState.COMPLETED
    assert outcome.text == "답"
    assert provider.call_count == 2          # 실패 1 + 성공 1


def test_retry_gives_up_after_the_limit(work: Path) -> None:
    """허용 횟수를 넘기면 포기하고 이유를 남긴다. 무한정 매달리지 않는다."""
    from harness.limits import Limits

    provider = FlakyProvider([ModelReply(text="답")], fail_times=9)
    limits = Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    outcome = make_agent(work, provider, limits=limits).run("해줘")

    assert outcome.state is TaskState.FAILED
    assert outcome.reason == "provider_connection"
    assert provider.call_count == 3          # 첫 호출 + 재시도 2회


def test_auth_error_is_not_retried(work: Path) -> None:
    """인증 오류는 다시 물어도 같은 답이 온다. 재시도하지 않는다."""
    from harness.limits import Limits

    provider = FlakyProvider([ModelReply(text="답")], fail_times=9, kind="auth")
    limits = Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    outcome = make_agent(work, provider, limits=limits).run("해줘")

    assert outcome.reason == "provider_auth"
    assert provider.call_count == 1


def test_retry_is_recorded(work: Path) -> None:
    """재시도했다는 사실이 기록에 남는다. 안 남으면 비교 근거가 없다."""
    from harness.limits import Limits

    provider = FlakyProvider([ModelReply(text="답")], fail_times=1)
    limits = Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    make_agent(work, provider, limits=limits).run("해줘")

    kinds = [e.get("kind") for e in events_of(work)]
    assert "provider_error" in kinds
    assert "provider_retry" in kinds


# ------------------------------------------- 6. 재시도 대상을 상태 코드로 가르기
#
# 2026-09-08 벤치마크 피드백: "400/422 같은 수정 없는 재요청과 일시적 연결
# 오류·429/5xx 를 구분해 재시도 정책을 검증하라."
#
# 그전에는 kind 만 봤다. "protocol" 한 이름 아래 400 과 502 가 섞여서,
# 컨텍스트 한도를 넘긴 요청(400)을 두 번 더 보내며 6초를 버린 기록이 있다
# (blockchain 문항 8/10 -> 1/10). 번호대로 4xx 를 통째로 제외하는 것도
# 틀린다 — 429 는 4xx 지만 기다리면 통한다.

import pytest as _pytest

from harness.limits import Limits as _Limits
from harness.providers import ProviderError, is_retryable


@_pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 413, 422])
def test_requests_that_need_fixing_are_not_retried(status: int) -> None:
    """고치지 않고 다시 보내면 같은 답이 오는 것들."""
    assert is_retryable(ProviderError("protocol", "x", status=status)) is False


@_pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_errors_are_retried(status: int) -> None:
    """기다렸다 보내면 달라질 수 있는 것들. 429 가 여기 들어가는 게 핵심이다."""
    assert is_retryable(ProviderError("protocol", "x", status=status)) is True


def test_connection_error_without_status_is_retried() -> None:
    """응답 자체가 없었으면 상태 코드도 없다. 다시 해볼 만하다."""
    assert is_retryable(ProviderError("connection", "끊김")) is True


def test_auth_error_is_never_retried_even_with_status() -> None:
    """인증은 다시 물어도 같은 답이 온다. 상태 코드와 무관하다."""
    assert is_retryable(ProviderError("auth", "x", status=401)) is False


def test_context_overflow_400_ends_without_retrying(work: Path) -> None:
    """400 을 받으면 재시도 없이 끝난다 — 벤치마크에서 6초를 버린 그 경로다."""
    provider = FlakyProvider([ModelReply(text="답")], fail_times=9,
                             kind="protocol", status=400)
    limits = _Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    outcome = make_agent(work, provider, limits=limits).run("해줘")

    assert outcome.state is TaskState.FAILED
    assert outcome.reason == "provider_protocol"
    assert provider.call_count == 1          # 첫 호출뿐, 재시도 없음


def test_rate_limit_429_is_retried_and_recovers(work: Path) -> None:
    """429 는 4xx 지만 재시도한다. 번호대로 잘랐다면 여기서 포기했을 것이다."""
    provider = FlakyProvider([ModelReply(text="답")], fail_times=1,
                             kind="protocol", status=429)
    limits = _Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    outcome = make_agent(work, provider, limits=limits).run("해줘")

    assert outcome.state is TaskState.COMPLETED
    assert provider.call_count == 2          # 실패 1 + 성공 1


def test_status_is_recorded_for_later_analysis(work: Path) -> None:
    """상태 코드가 기록에 남아야 나중에 무엇 때문에 끝났는지 가릴 수 있다."""
    provider = FlakyProvider([ModelReply(text="답")], fail_times=9,
                             kind="protocol", status=400)
    limits = _Limits(max_provider_retries=2, provider_retry_backoff_seconds=0.01)
    make_agent(work, provider, limits=limits).run("해줘")

    errors = [e for e in events_of(work) if e.get("kind") == "provider_error"]
    assert errors and errors[0]["status"] == 400
