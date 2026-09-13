"""모델 제공자 연결부.

하네스 안에서는 제공자와 무관한 형태로 주고받는다. 각 제공자의 요청·응답을
그 내부 형태로 옮기는 일이 이 파일의 몫이다. (R07 / INTERFACES.md 모델 제공자 연결부)

내부 대화 형식:
    {"role": "user",      "content": "..."}
    {"role": "assistant", "content": "...", "tool_requests": [ToolRequest, ...]}
    {"role": "tool",      "tool_name": "...", "content": "..."}
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ProviderError(Exception):
    """모델과 이야기하다 생긴 문제.

    kind 로 원인을 나눈다. 연결이 안 된 것과 응답이 이상한 것은
    고쳐야 할 곳이 다르다. (TROUBLESHOOTING.md 증상표)

    status 는 HTTP 응답이 있었을 때의 상태 코드다. 연결 자체가 안 됐거나
    응답을 파싱하지 못한 경우에는 None 이다.

    이 값이 필요한 이유는 2026-09-08 벤치마크에서 드러났다. kind 만으로는
    "protocol" 한 이름 아래 400(요청이 잘못됨)과 502(서버가 잠깐 죽음)가
    섞인다. 그 둘을 똑같이 재시도했더니, 컨텍스트 한도를 넘긴 요청을
    두 번 더 보내며 6초를 버렸다. 다시 보내서 답이 달라질 오류인지는
    상태 코드를 봐야 갈린다.
    """

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # "connection" | "auth" | "protocol"
        self.message = message
        self.status = status


# 다시 보내도 같은 답이 오는 상태 코드. 요청 자체를 고치지 않으면 소용없다.
#
# 4xx/5xx 로 가르지 않는 이유: 429 는 4xx 인데 기다렸다 보내면 통한다.
# 기준은 "고치지 않고 다시 보내면 답이 달라지는가" 이지 번호대가 아니다.
NON_RETRYABLE_STATUS = frozenset({
    400,  # 요청 형식이 틀렸거나 컨텍스트 한도를 넘음
    401,  # 인증 없음
    403,  # 권한 없음
    404,  # 없는 모델·경로
    405,  # 허용하지 않는 메서드
    409,  # 상태 충돌
    413,  # 본문이 너무 큼
    422,  # 형식은 맞지만 내용을 처리할 수 없음
})


def is_retryable(error: "ProviderError") -> bool:
    """이 오류를 다시 시도할 가치가 있는가.

    - 인증 오류는 다시 물어도 같은 답이 온다.
    - 상태 코드가 없으면 연결이 끊기거나 시간이 초과된 것이다. 다시 해볼 만하다.
    - 429(호출량 제한)와 408(요청 시간 초과)은 4xx 지만 기다리면 통한다.
    - 그 밖의 4xx 는 요청을 고쳐야 하므로 재시도하지 않는다.
    - 5xx 는 서버 쪽 문제라 잠시 뒤 통할 수 있다.
    """
    if error.kind == "auth":
        return False
    if error.status is None:
        return error.kind in {"connection", "protocol"}
    return error.status not in NON_RETRYABLE_STATUS


@dataclass(frozen=True)
class ToolRequest:
    """모델이 낸 도구 요청. arguments 는 언제나 딕셔너리다."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    """모델의 한 번 응답. 최종 텍스트이거나 도구 요청 목록이다."""

    text: str = ""
    tool_requests: list[ToolRequest] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_requests)


class Provider(Protocol):
    """제공자가 지켜야 할 약속. 이것만 맞으면 하네스는 누구든 쓴다."""

    name: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
    ) -> ModelReply: ...


def normalize_arguments(raw: Any, tool_name: str) -> dict[str, Any]:
    """제공자마다 다른 arguments 를 딕셔너리로 맞춘다.

    2026-09-08 실측:
      - Ollama(/api/chat) 는 파싱된 객체를 준다.  {"path": "posts/a.md"}
      - OpenAI 호환 API 는 JSON 문자열을 준다.    "{\"path\": \"posts/a.md\"}"

    같은 필드를 보내면 같은 게 온다고 가정하지 않는다. 여기서 흡수한다.
    """
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "protocol",
                f"{tool_name} 의 arguments 를 JSON 으로 읽지 못했습니다: {error}",
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderError(
                "protocol",
                f"{tool_name} 의 arguments 가 객체가 아닙니다: {type(parsed).__name__}",
            )
        return parsed

    if raw is None:
        return {}

    raise ProviderError(
        "protocol",
        f"{tool_name} 의 arguments 형태를 다루지 못합니다: {type(raw).__name__}",
    )


class OllamaProvider:
    """로컬 Ollama. D04 의 첫 제공자.

    POST /api/chat 을 쓴다. OpenAI 호환 경로가 아니다.
    """

    name = "ollama"

    def __init__(
        self,
        model: str = "qwen3.5:2b",
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 300.0,
        keep_alive: str = "30m",
        num_thread: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

        # CPU 스레드 수. 2026-09-08: 벤치마크를 돌리는 동안 노트북이 멈춰서
        # 다른 작업을 할 수 없었다. 환경 변수 OLLAMA_NUM_THREAD 는 서버가
        # 시작할 때만 읽으므로 이미 떠 있는 서버에는 안 먹는다. 요청마다
        # 지정하면 서버를 건드리지 않고 조절할 수 있고, 몇 개를 썼는지가
        # 측정 조건으로 기록에도 남는다.
        self.num_thread = num_thread

        # Ollama 는 기본 5분이 지나면 모델을 메모리에서 내린다.
        # 이 기계에서는 다시 올리는 데 약 51초가 걸려서, 실제로 재는
        # 시간의 대부분이 적재 시간이 되어 버린다. 실습 동안은 붙잡아 둔다.
        self.keep_alive = keep_alive

    def _to_wire(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """내부 대화 형식을 Ollama 가 받는 형태로 옮긴다."""
        wire: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]

            if role == "assistant" and message.get("tool_requests"):
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": [
                            {
                                "id": request.id,
                                "function": {
                                    "name": request.name,
                                    "arguments": request.arguments,
                                },
                            }
                            for request in message["tool_requests"]
                        ],
                    }
                )
            elif role == "tool":
                # Ollama 는 tool_call_id 가 아니라 tool_name 으로 짝을 맞춘다.
                wire.append(
                    {
                        "role": "tool",
                        "tool_name": message["tool_name"],
                        "content": message["content"],
                    }
                )
            else:
                wire.append({"role": role, "content": message.get("content", "")})
        return wire

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
    ) -> ModelReply:
        body = {
            "model": self.model,
            "messages": self._to_wire(messages),
            "tools": tool_definitions,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
        }

        if self.num_thread:
            body["options"] = {"num_thread": self.num_thread}

        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=body,
                timeout=self.timeout_seconds,
            )
        except httpx.ConnectError as error:
            raise ProviderError(
                "connection",
                f"Ollama 서버에 연결하지 못했습니다 ({self.base_url}). "
                f"ollama 가 실행 중인지 확인하세요. 원인: {error}",
            ) from None
        except httpx.TimeoutException:
            raise ProviderError(
                "connection",
                f"Ollama 응답이 {self.timeout_seconds}초 안에 오지 않았습니다.",
            ) from None

        if response.status_code == 401 or response.status_code == 403:
            raise ProviderError(
                "auth",
                f"인증에 실패했습니다 (HTTP {response.status_code}).",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise ProviderError(
                "protocol",
                f"Ollama 가 HTTP {response.status_code} 를 돌려주었습니다: {response.text[:300]}",
                status=response.status_code,
            )

        try:
            data = response.json()
        except json.JSONDecodeError:
            raise ProviderError("protocol", "응답이 JSON 이 아닙니다.") from None

        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError("protocol", "응답에 message 가 없습니다.")

        requests: list[ToolRequest] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_name = function.get("name")
            if not tool_name:
                raise ProviderError("protocol", "도구 요청에 이름이 없습니다.")

            # Ollama 0.33.3 은 id 를 준다. 없는 버전을 대비해 직접 채운다.
            call_id = call.get("id") or f"local_{index}_{uuid.uuid4().hex[:8]}"

            requests.append(
                ToolRequest(
                    id=call_id,
                    name=tool_name,
                    arguments=normalize_arguments(function.get("arguments"), tool_name),
                )
            )

        return ModelReply(text=message.get("content") or "", tool_requests=requests)


class OpenAICompatProvider:
    """OpenAI 호환 API 를 쓰는 제공자. Colab 의 vLLM 이 이 형식을 연다. (D08)

    Ollama 와 무엇이 다른지가 이 클래스의 존재 이유다.

        경로        /v1/chat/completions      (Ollama 는 /api/chat)
        arguments   JSON 문자열                (Ollama 는 객체)
        도구 결과   tool_call_id 로 짝을 맞춤   (Ollama 는 tool_name)

    같은 필드를 보내면 같은 게 온다고 가정하지 않는다. 여기서 흡수한다.
    """

    name = "vllm"

    def __init__(
        self,
        base_url: str,
        model: str = "cyankiwi/Qwen3.5-4B-AWQ-4bit",
        api_key: str | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _to_wire(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """내부 대화 형식을 OpenAI 형식으로 옮긴다.

        tool 메시지가 어느 호출에 대한 답인지 tool_call_id 로 이어야 한다.
        내부 형식에는 그 값이 tool 메시지에 없으므로, 바로 앞 assistant 메시지의
        도구 요청에서 이름이 같은 것을 찾아 짝을 짓는다.
        """
        wire: list[dict[str, Any]] = []
        pending: dict[str, str] = {}  # 도구 이름 -> 호출 id

        for message in messages:
            role = message["role"]

            if role == "assistant" and message.get("tool_requests"):
                requests = message["tool_requests"]
                pending = {request.name: request.id for request in requests}
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or None,
                        "tool_calls": [
                            {
                                "id": request.id,
                                "type": "function",
                                "function": {
                                    "name": request.name,
                                    # 이쪽은 문자열이어야 한다.
                                    "arguments": json.dumps(
                                        request.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for request in requests
                        ],
                    }
                )
            elif role == "tool":
                tool_name = message["tool_name"]
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": pending.get(tool_name, tool_name),
                        "content": message["content"],
                    }
                )
            else:
                wire.append({"role": role, "content": message.get("content", "")})
        return wire

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
    ) -> ModelReply:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": self.model,
            "messages": self._to_wire(messages),
            "tools": tool_definitions,
            "tool_choice": "auto",
            "stream": False,
        }

        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except httpx.ConnectError as error:
            raise ProviderError(
                "connection",
                f"모델 서버에 연결하지 못했습니다. 터널이 살아 있는지 확인하세요. 원인: {error}",
            ) from None
        except httpx.TimeoutException:
            raise ProviderError(
                "connection", f"응답이 {self.timeout_seconds}초 안에 오지 않았습니다."
            ) from None

        if response.status_code in (401, 403):
            raise ProviderError(
                "auth",
                f"인증에 실패했습니다 (HTTP {response.status_code}).",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise ProviderError(
                "protocol",
                f"서버가 HTTP {response.status_code} 를 돌려주었습니다: {response.text[:300]}",
                status=response.status_code,
            )

        try:
            data = response.json()
        except json.JSONDecodeError:
            raise ProviderError("protocol", "응답이 JSON 이 아닙니다.") from None

        choices = data.get("choices")
        if not choices:
            raise ProviderError("protocol", "응답에 choices 가 없습니다.")

        message = choices[0].get("message") or {}

        requests: list[ToolRequest] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_name = function.get("name")
            if not tool_name:
                raise ProviderError("protocol", "도구 요청에 이름이 없습니다.")
            requests.append(
                ToolRequest(
                    id=call.get("id") or f"call_{index}_{uuid.uuid4().hex[:8]}",
                    name=tool_name,
                    # 여기서 문자열이 온다. normalize_arguments 가 파싱한다.
                    arguments=normalize_arguments(function.get("arguments"), tool_name),
                )
            )

        return ModelReply(text=message.get("content") or "", tool_requests=requests)


class FakeProvider:
    """모델을 부르지 않는 가짜 제공자.

    미리 정해 둔 응답을 순서대로 돌려준다. 실제 모델을 쓰기 전에
    하네스 쪽 흐름이 맞는지부터 확인하려고 쓴다. (A02, A06, A07)

    처음부터 실제 모델로 확인하면 실패했을 때 '내 코드가 틀렸나,
    모델이 도구를 안 골랐나' 를 가릴 수 없다.
    """

    name = "fake"

    def __init__(self, replies: list[ModelReply], repeat_last: bool = False) -> None:
        self.replies = list(replies)
        self.repeat_last = repeat_last
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
    ) -> ModelReply:
        self.calls.append(list(messages))

        if self.replies:
            if len(self.replies) == 1 and self.repeat_last:
                # A06: 도구를 계속 부르는 모델을 흉내 낸다. 한도가 걸리는지 본다.
                return self.replies[0]
            return self.replies.pop(0)

        raise ProviderError("protocol", "FakeProvider 에 준비된 응답이 없습니다.")
