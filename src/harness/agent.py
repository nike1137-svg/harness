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
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS, ToolError

# 승인을 받아야 하는 도구. 읽기는 경로 검사만으로 통과한다. (D06)
APPROVAL_REQUIRED = {"write_file", "edit_file", "run_command"}


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskOutcome:
    """작업 하나의 결말. 미완료를 완료로 보여 주지 않는다."""

    state: TaskState
    text: str
    reason: str
    tool_calls_used: int
    elapsed_seconds: float
    messages: list[dict[str, Any]] = field(default_factory=list)

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

    # ------------------------------------------------------------------ 루프

    def run(
        self,
        request: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> TaskOutcome:
        if not request or not request.strip():
            return TaskOutcome(TaskState.FAILED, "", "empty_request", 0, 0.0, messages or [])

        started = time.monotonic()
        conversation: list[dict[str, Any]] = list(messages or [])
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
            elapsed = time.monotonic() - started
            if elapsed > self.limits.task_timeout_seconds:
                return self._finish(
                    TaskState.FAILED, "", "task_timeout", tool_calls_used, started, conversation
                )

            # --- 모델에게 묻는다 -------------------------------------------
            try:
                reply: ModelReply = self.provider.complete(conversation, TOOL_DEFINITIONS)
            except ProviderError as error:
                self.recorder.write(
                    "provider_error", error_kind=error.kind, message=error.message
                )
                return self._finish(
                    TaskState.FAILED,
                    "",
                    f"provider_{error.kind}",
                    tool_calls_used,
                    started,
                    conversation,
                )

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

            if not self.approver(request):
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
        self.recorder.write(
            "tool_result",
            name=request.name,
            ok=True,
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
        elapsed = time.monotonic() - started
        self.recorder.write(
            "task_end",
            state=state.value,
            reason=reason,
            tool_calls_used=tool_calls_used,
            elapsed_seconds=round(elapsed, 2),
        )
        return TaskOutcome(state, text, reason, tool_calls_used, elapsed, conversation)
