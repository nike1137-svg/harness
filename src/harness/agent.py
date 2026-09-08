"""반복 루프. 이 하네스의 본체다. (R02)

    사용자 요청 → 모델 호출 → 응답 종류 확인
        ├ 도구 요청 → 이름·인자·권한 검사 → (승인) → 실행 → 결과를 대화에 추가 → 다시 모델
        └ 최종 답변 → 완료
        한도 초과 / 연결 실패 → 중단

이 반복을 다른 에이전트 도구에 맡기지 않고 직접 구현한다. 그것이 과제의 조건이다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .limits import DEFAULT_LIMITS, Limits
from .providers import ModelReply, Provider, ProviderError, ToolRequest
from .session import Recorder
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS, TOOL_PRECHECKS, ToolError

# 승인을 받아야 하는 도구. 읽기는 경로 검사만으로 통과한다. (D06)
APPROVAL_REQUIRED = {"write_file", "edit_file", "run_python", "run_command"}


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


# 모델에게 주는 지침. 2026-09-08 까지 이것이 없어서 모델이 사용자 요청만
# 받고 있었다. 그날 4B 모델이 "위와 같이 내용을 수정하겠습니다" 라고 쓰고
# 도구를 부르지 않은 채 대화를 끝냈다. 파일은 그대로였는데 상태는 completed 였다.
SYSTEM_PROMPT = """당신은 블로그 글과 그 검사 코드를 손보는 일을 돕는다.
도구를 써서 실제로 일한다. 다음을 반드시 지킨다.

1. 말만 하고 끝내지 마라. "고치겠습니다", "수정하겠습니다" 라고 쓰고 대화를
   마치면 아무것도 하지 않은 것이다. 고칠 것이 있으면 edit_file 을 실제로 불러라.
2. 코드를 고칠 때는 write_file 이 아니라 edit_file 을 쓴다. write_file 은 파일
   전체를 덮어쓰므로 고치라고 하지 않은 곳까지 망가뜨린다.
3. edit_file 의 old_string 은 read_file 로 본 글자 그대로 보낸다. 따옴표를
   역슬래시로 감싸지 마라. 파일에 ("title", "date") 라고 있으면 그대로 보낸다.
4. 도구가 실패하면 오류 메시지를 읽고 원인을 고쳐서 다시 시도한다. 같은 인자를
   그대로 다시 보내지 마라.
5. 시키지 않은 것은 건드리지 않는다. 요청된 곳만 바꾼다.
6. 답에는 도구로 확인한 내용만 쓴다. 읽지 않은 것을 짐작해서 말하지 마라.
"""

# 파일을 실제로 바꾸는 도구. 몇 건 바뀌었는지 세어 사용자에게 보여 준다.
WRITE_TOOLS = {"write_file", "edit_file"}


@dataclass
class TaskOutcome:
    """작업 하나의 결말. 미완료를 완료로 보여 주지 않는다."""

    state: TaskState
    text: str
    reason: str
    tool_calls_used: int
    elapsed_seconds: float
    messages: list[dict[str, Any]] = field(default_factory=list)

    # 이 작업에서 실제로 바뀐 파일 수. completed 라도 이 값이 0 이면
    # 모델이 하겠다고만 하고 끝냈을 수 있다. 화면에 함께 찍는다.
    files_changed: int = 0

    @property
    def succeeded(self) -> bool:
        return self.state is TaskState.COMPLETED


# 승인을 묻는 함수의 모양. True 면 승인, False 면 거절.
Approver = Callable[[ToolRequest], bool]


def deny_all(request: ToolRequest) -> bool:
    """기본값은 거절이다.

    승인 함수를 깜빡 넘기지 않았을 때 조용히 실행되는 쪽보다,
    아무것도 못 하는 쪽이 안전하다.
    """
    return False


class Agent:
    def __init__(
        self,
        provider: Provider,
        work_root: Path,
        recorder: Recorder,
        limits: Limits = DEFAULT_LIMITS,
        approver: Approver = deny_all,
    ) -> None:
        self.provider = provider
        self.work_root = work_root
        self.recorder = recorder
        self.limits = limits
        self.approver = approver

        # 사용자가 승인 화면을 보고 있던 시간. 작업 시간에서 뺀다.
        # 2026-09-08: 이 처리가 없어서 A03 이 task_timeout 으로 실패했다.
        # 승인을 신중히 읽을수록 작업이 죽는 구조였다. 사람이 판단하는 시간은
        # 하네스가 일한 시간이 아니다.
        self._approval_seconds = 0.0

        # 이 작업에서 실제로 바뀐 파일 수.
        self._files_changed = 0

    # ------------------------------------------------------------------ 루프

    def run(
        self,
        request: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> TaskOutcome:
        if not request or not request.strip():
            return TaskOutcome(TaskState.FAILED, "", "empty_request", 0, 0.0, messages or [])

        started = time.monotonic()
        self._approval_seconds = 0.0
        self._files_changed = 0
        conversation: list[dict[str, Any]] = list(messages or [])

        # 세션을 이어가는 경우 지침이 이미 대화 앞머리에 있다. 두 번 넣지 않는다.
        if not any(message.get("role") == "system" for message in conversation):
            conversation.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

        conversation.append({"role": "user", "content": request})

        self.recorder.write(
            "task_start",
            provider=self.provider.name,
            request=request,
            continued=bool(messages),
        )

        tool_calls_used = 0
        rejected: set[tuple[str, str]] = set()

        while True:
            # 승인을 기다린 시간은 빼고 센다. 사람이 판단하는 동안은
            # 하네스가 일하는 중이 아니다.
            elapsed = time.monotonic() - started - self._approval_seconds
            if elapsed > self.limits.task_timeout_seconds:
                return self._finish(
                    TaskState.FAILED, "", "task_timeout", tool_calls_used, started, conversation
                )

            # --- 모델에게 묻는다 -------------------------------------------
            reply: ModelReply | None = None
            attempt = 0

            while reply is None:
                # 모델 호출 하나가 남은 예산을 넘기지 않게 한다.
                #
                # 2026-09-08 벤치마크 1·2차: 한도 검사는 호출이 끝난 뒤에만
                # 하므로, 예산이 얼마 안 남은 상태에서 시작한 호출이 예산을
                # 훌쩍 넘겼다. 그 사이 평가 도구가 먼저 끊어 이 하네스의
                # 종료 이유와 사용량이 통째로 버려졌다(10문항 중 9개).
                # 끊더라도 우리가 끊어야 무엇에 걸렸는지 남길 수 있다.
                elapsed = time.monotonic() - started - self._approval_seconds
                remaining = self.limits.task_timeout_seconds - elapsed
                if hasattr(self.provider, "timeout_seconds"):
                    self.provider.timeout_seconds = max(15.0, remaining - 3.0)

                try:
                    reply = self.provider.complete(conversation, TOOL_DEFINITIONS)
                except ProviderError as error:
                    self.recorder.write(
                        "provider_error",
                        error_kind=error.kind,
                        message=error.message,
                        attempt=attempt,
                    )

                    # 인증 오류는 다시 물어도 같은 답이 온다.
                    retryable = error.kind in {"connection", "protocol"}
                    wait = self.limits.provider_retry_backoff_seconds * (2**attempt)
                    # 기다린 뒤 호출할 시간이 남아 있어야 재시도할 값이 있다.
                    room_left = remaining - wait > 20.0

                    if (
                        not retryable
                        or attempt >= self.limits.max_provider_retries
                        or not room_left
                    ):
                        return self._finish(
                            TaskState.FAILED,
                            "",
                            f"provider_{error.kind}",
                            tool_calls_used,
                            started,
                            conversation,
                        )

                    attempt += 1
                    self.recorder.write(
                        "provider_retry", error_kind=error.kind, attempt=attempt,
                        wait_seconds=round(wait, 1),
                    )
                    time.sleep(wait)

            # --- 응답 종류를 가른다 ----------------------------------------
            if not reply.wants_tools:
                conversation.append({"role": "assistant", "content": reply.text})
                self.recorder.write("final_answer", chars=len(reply.text))
                return self._finish(
                    TaskState.COMPLETED,
                    reply.text,
                    "final_answer",
                    tool_calls_used,
                    started,
                    conversation,
                )

            # 도구를 부르기로 한 응답도 대화에 남긴다.
            # 이걸 빠뜨리면 모델이 자기가 무엇을 요청했는지 잊는다.
            conversation.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    "tool_requests": list(reply.tool_requests),
                }
            )

            # --- 도구 요청을 받은 순서대로 하나씩 처리한다 -----------------
            for tool_request in reply.tool_requests:
                tool_calls_used += 1
                if tool_calls_used > self.limits.max_tool_calls:
                    self.recorder.write("tool_limit", used=tool_calls_used - 1)
                    return self._finish(
                        TaskState.FAILED,
                        "",
                        "tool_limit",
                        tool_calls_used - 1,
                        started,
                        conversation,
                    )

                result_text = self._handle_tool(tool_request, rejected)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_name": tool_request.name,
                        "content": result_text,
                    }
                )

    # ------------------------------------------------------------- 도구 하나

    def _handle_tool(self, request: ToolRequest, rejected: set[tuple[str, str]]) -> str:
        """도구 하나를 검사하고 실행한다. 언제나 문자열을 돌려준다.

        오류도 결과다. 예외로 루프를 끊지 않고 모델에게 돌려주어
        모델이 무엇이 잘못됐는지 읽고 고칠 기회를 준다.
        """
        self.recorder.write("tool_request", name=request.name, arguments=request.arguments)

        handler = TOOL_HANDLERS.get(request.name)
        if handler is None:
            return self._tool_failure(
                request, "UNKNOWN_TOOL", f"그런 도구는 없습니다: {request.name}"
            )

        # --- 승인 전 검사 -----------------------------------------------
        # 파일에 없는 문자열을 바꾸겠다거나 허용 목록에 없는 명령을 돌리겠다는
        # 요청은 승인을 물을 가치가 없다. 사용자를 부르기 전에 걸러 낸다.
        precheck = TOOL_PRECHECKS.get(request.name)
        if precheck is not None:
            try:
                precheck(self.work_root, request.arguments)
            except ToolError as error:
                return self._tool_failure(request, error.code, error.message)

        # --- 승인 -------------------------------------------------------
        if request.name in APPROVAL_REQUIRED:
            key = (request.name, json.dumps(request.arguments, sort_keys=True, ensure_ascii=False))

            if key in rejected:
                # 거절된 것을 다시 들이미는 흐름을 막는다. (D06)
                return self._tool_failure(
                    request,
                    "DUPLICATE_REJECTED",
                    "이미 거절된 요청입니다. 같은 내용으로 다시 요청하지 마세요.",
                )

            waiting_from = time.monotonic()
            granted = self.approver(request)
            self._approval_seconds += time.monotonic() - waiting_from

            if not granted:
                rejected.add(key)
                self.recorder.write("approval_rejected", name=request.name)
                return self._tool_failure(
                    request, "REJECTED_BY_USER", "사용자가 이 작업을 거절했습니다."
                )

            self.recorder.write("approval_granted", name=request.name)

        # --- 실행 -------------------------------------------------------
        try:
            result = handler(self.work_root, request.arguments, self.limits)
        except ToolError as error:
            return self._tool_failure(request, error.code, error.message)

        payload = result.to_model()

        # 파일을 실제로 바꾼 도구가 성공했을 때만 센다.
        if request.name in WRITE_TOOLS and result.ok:
            self._files_changed += 1

        self.recorder.write(
            "tool_result",
            name=request.name,
            # result.ok 를 그대로 넘긴다. 2026-09-08 까지 여기에 True 가
            # 못박혀 있어서, pytest 가 실패해도 화면에 "성공" 으로 찍혔다.
            # run_command 에서 ok=(종료코드==0) 으로 만들어 놓고 그 값을 버렸다.
            ok=result.ok,
            summary={key: value for key, value in payload.items() if key != "text"},
        )
        return json.dumps(payload, ensure_ascii=False)

    def _tool_failure(self, request: ToolRequest, code: str, message: str) -> str:
        self.recorder.write("tool_result", name=request.name, ok=False, code=code)
        return json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False)

    # ---------------------------------------------------------------- 마무리

    def _finish(
        self,
        state: TaskState,
        text: str,
        reason: str,
        tool_calls_used: int,
        started: float,
        conversation: list[dict[str, Any]],
    ) -> TaskOutcome:
        elapsed = time.monotonic() - started - self._approval_seconds
        self.recorder.write(
            "task_end",
            state=state.value,
            reason=reason,
            tool_calls_used=tool_calls_used,
            elapsed_seconds=round(elapsed, 2),
            approval_wait_seconds=round(self._approval_seconds, 2),
            files_changed=self._files_changed,
        )
        return TaskOutcome(
            state,
            text,
            reason,
            tool_calls_used,
            elapsed,
            conversation,
            files_changed=self._files_changed,
        )
