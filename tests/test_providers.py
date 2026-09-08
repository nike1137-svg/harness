"""제공자 어댑터가 차이를 흡수하는지 본다. (R07 / D04·D08 / A10)

서버에 연결하지 않는다. 같은 내부 대화가 두 제공자에게
서로 다른 형태로 나가는지만 확인한다.
"""

from __future__ import annotations

import json

from harness.providers import (
    OllamaProvider,
    OpenAICompatProvider,
    ToolRequest,
)


def conversation() -> list[dict]:
    """같은 내부 대화 하나. 이것을 두 제공자에게 각각 넘긴다."""
    return [
        {"role": "user", "content": "posts/ok.md 읽어줘"},
        {
            "role": "assistant",
            "content": "",
            "tool_requests": [ToolRequest("call_1", "read_file", {"path": "posts/ok.md"})],
        },
        {"role": "tool", "tool_name": "read_file", "content": '{"ok": true}'},
    ]


def test_ollama_sends_arguments_as_an_object() -> None:
    """Ollama 는 파싱된 객체를 주고받는다. (2026-09-08 실측)"""
    wire = OllamaProvider()._to_wire(conversation())

    arguments = wire[1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, dict)
    assert arguments == {"path": "posts/ok.md"}


def test_ollama_matches_tool_results_by_name() -> None:
    wire = OllamaProvider()._to_wire(conversation())

    assert wire[2]["role"] == "tool"
    assert wire[2]["tool_name"] == "read_file"
    assert "tool_call_id" not in wire[2]


def test_openai_compat_sends_arguments_as_a_json_string() -> None:
    """OpenAI 호환 API 는 문자열을 요구한다. 같은 대화가 다르게 나간다."""
    wire = OpenAICompatProvider(base_url="http://example.invalid")._to_wire(conversation())

    arguments = wire[1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"path": "posts/ok.md"}


def test_openai_compat_matches_tool_results_by_call_id() -> None:
    """이름이 아니라 호출 id 로 짝을 맞춘다."""
    wire = OpenAICompatProvider(base_url="http://example.invalid")._to_wire(conversation())

    assert wire[2]["role"] == "tool"
    assert wire[2]["tool_call_id"] == "call_1"
    assert "tool_name" not in wire[2]


def test_same_conversation_produces_different_wire_formats() -> None:
    """한 대화가 두 형태로 갈린다. 어댑터가 필요한 이유가 이것이다."""
    same = conversation()
    ollama_wire = OllamaProvider()._to_wire(same)
    openai_wire = OpenAICompatProvider(base_url="http://example.invalid")._to_wire(same)

    assert ollama_wire != openai_wire

    ollama_args = ollama_wire[1]["tool_calls"][0]["function"]["arguments"]
    openai_args = openai_wire[1]["tool_calls"][0]["function"]["arguments"]
    assert type(ollama_args) is not type(openai_args)


def test_provider_names_are_distinct() -> None:
    assert OllamaProvider().name == "ollama"
    assert OpenAICompatProvider(base_url="http://example.invalid").name == "vllm"
