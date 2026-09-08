"""승인·거절과 명령 실행 검증. (ACCEPTANCE A03·A04 / R04·R05 / D05·D06)

A04 의 판정 기준은 '거절 버튼이 있다' 가 아니라
'거절 전후 파일 내용이 같다' 다. (examples/spec-example.md)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness.agent import Agent, TaskState
from harness.limits import Limits
from harness.providers import FakeProvider, ModelReply, ToolRequest
from harness.session import Recorder
from harness.tools import ToolError, edit_file, run_command, write_file


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "checker.py").write_text("원래 내용\n", encoding="utf-8")
    return root


def make_agent(work: Path, provider, **kwargs) -> Agent:
    recorder = Recorder(work.parent / "events.jsonl")
    return Agent(provider=provider, work_root=work, recorder=recorder, **kwargs)


def tool_messages(outcome) -> list[dict]:
    return [m for m in outcome.messages if m["role"] == "tool"]


def payload_of(message: dict) -> dict:
    return json.loads(message["content"])


def approve_all(request: ToolRequest) -> bool:
    return True


def reject_all(request: ToolRequest) -> bool:
    return False


WRITE_ARGS = {"path": "checker.py", "content": "고친 내용\n"}


# --------------------------------------------------------------- A04

def test_a04_rejected_write_leaves_file_byte_identical(work: Path) -> None:
    """거절하면 파일이 바이트 단위로 그대로여야 한다."""
    target = work / "checker.py"
    before = target.read_bytes()

    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "write_file", WRITE_ARGS)]),
            ModelReply(text="거절되어 바꾸지 못했습니다."),
        ]
    )
    outcome = make_agent(work, provider, approver=reject_all).run("checker.py 고쳐줘")

    assert target.read_bytes() == before

    result = payload_of(tool_messages(outcome)[0])
    assert result["ok"] is False
    assert result["code"] == "REJECTED_BY_USER"

    # 거절은 실패가 아니다. 모델이 거절 사실을 받고 계속 진행한다. (D06)
    assert outcome.state is TaskState.COMPLETED


def test_a04_approved_write_actually_changes_the_file(work: Path) -> None:
    """승인하면 실제로 바뀐다. 대조군이다."""
    target = work / "checker.py"

    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "write_file", WRITE_ARGS)]),
            ModelReply(text="고쳤습니다."),
        ]
    )
    make_agent(work, provider, approver=approve_all).run("고쳐줘")

    assert target.read_text(encoding="utf-8") == "고친 내용\n"


def test_a04_same_request_after_rejection_is_blocked_without_asking(work: Path) -> None:
    """거절된 것을 다시 들이밀면 사용자에게 묻지 않고 막는다. (D06)"""
    asked: list[str] = []

    def reject_and_count(request: ToolRequest) -> bool:
        asked.append(request.name)
        return False

    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "write_file", WRITE_ARGS)]),
            # 인자가 완전히 같은 재요청. id 만 다르다.
            ModelReply(tool_requests=[ToolRequest("c2", "write_file", dict(WRITE_ARGS))]),
            ModelReply(text="포기합니다."),
        ]
    )
    outcome = make_agent(work, provider, approver=reject_and_count).run("고쳐줘")

    # 사용자에게는 한 번만 물었다.
    assert len(asked) == 1

    results = tool_messages(outcome)
    assert payload_of(results[0])["code"] == "REJECTED_BY_USER"
    assert payload_of(results[1])["code"] == "DUPLICATE_REJECTED"


def test_write_outside_work_is_refused_even_if_approved(work: Path) -> None:
    """승인을 받아도 경로 검사는 따로 적용된다."""
    provider = FakeProvider(
        [
            ModelReply(
                tool_requests=[
                    ToolRequest("c1", "write_file", {"path": "../새파일.txt", "content": "x"})
                ]
            ),
            ModelReply(text="쓰지 못했습니다."),
        ]
    )
    outcome = make_agent(work, provider, approver=approve_all).run("써줘")

    assert payload_of(tool_messages(outcome)[0])["code"] == "PATH_OUTSIDE_WORK"
    assert not (work.parent / "새파일.txt").exists()


def test_default_approver_denies(work: Path) -> None:
    """승인 함수를 넘기지 않으면 아무것도 실행되지 않는다."""
    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "write_file", WRITE_ARGS)]),
            ModelReply(text="끝"),
        ]
    )
    outcome = make_agent(work, provider).run("고쳐줘")  # approver 생략
    assert payload_of(tool_messages(outcome)[0])["code"] == "REJECTED_BY_USER"
    assert (work / "checker.py").read_text(encoding="utf-8") == "원래 내용\n"


# ------------------------------------------------- run_command 권한 (D05)

@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "."],
        ["python", "-c", "print(1)"],
        ["cmd", "/c", "dir"],
        ["powershell", "-Command", "ls"],
    ],
)
def test_command_outside_allowlist_is_refused(work: Path, argv: list[str]) -> None:
    with pytest.raises(ToolError) as caught:
        run_command(work, {"argv": argv})
    assert caught.value.code == "COMMAND_NOT_ALLOWED"


def test_argv_must_be_a_list_not_a_sentence(work: Path) -> None:
    """한 문장으로 보내면 거부한다.

    문자열을 받아 셸에 넘기면 'pytest tests/; rm -rf .' 같은 것이 통한다.
    목록만 받는 것이 그 통로를 막는 방법이다.
    """
    with pytest.raises(ToolError) as caught:
        run_command(work, {"argv": "pytest tests/"})
    assert caught.value.code == "BAD_ARGUMENTS"


def test_argv_items_must_be_strings(work: Path) -> None:
    with pytest.raises(ToolError) as caught:
        run_command(work, {"argv": ["pytest", 123]})
    assert caught.value.code == "BAD_ARGUMENTS"


def test_empty_argv_is_refused(work: Path) -> None:
    with pytest.raises(ToolError) as caught:
        run_command(work, {"argv": []})
    assert caught.value.code == "BAD_ARGUMENTS"


def test_shell_metacharacters_stay_as_plain_arguments(work: Path) -> None:
    """; 를 인자에 넣어도 두 번째 명령으로 갈라지지 않는다.

    셸을 거치지 않으므로 pytest 가 그 글자를 경로로 받아 실패할 뿐이다.
    중요한 건 rm 이 실행되지 않는다는 것이다.
    """
    canary = work / "지워지면안됨.txt"
    canary.write_text("살아 있어야 한다", encoding="utf-8")

    result = run_command(work, {"argv": ["pytest", ";", "rm", "-rf", "."]})

    assert result.ok is False           # pytest 가 그 인자를 못 알아듣는다
    assert canary.exists()              # 파일은 그대로다


# ------------------------------------------------- run_command 결과 판정

def test_failing_test_is_reported_as_not_ok(work: Path) -> None:
    """테스트가 실패하면 ok=False 다.

    '실행은 됐다' 를 성공으로 돌려주면 모델이 그것만 보고 다 됐다고 한다.
    강의가 경계하는 실패다.
    """
    tests = work / "tests"
    tests.mkdir()
    (tests / "test_fail.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    result = run_command(work, {"argv": ["pytest", "tests/", "-q"]})

    assert result.ok is False
    assert result.payload["exit_code"] != 0
    assert "assert" in result.payload["stdout"] or "failed" in result.payload["stdout"]


def test_passing_test_is_reported_as_ok(work: Path) -> None:
    tests = work / "tests"
    tests.mkdir()
    (tests / "test_pass.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    result = run_command(work, {"argv": ["pytest", "tests/", "-q"]})

    assert result.ok is True
    assert result.payload["exit_code"] == 0


def test_command_runs_inside_work_folder(work: Path) -> None:
    """명령의 작업 디렉터리가 work/ 로 고정되는가."""
    tests = work / "tests"
    tests.mkdir()
    (tests / "test_cwd.py").write_text(
        "from pathlib import Path\n"
        "def test_cwd():\n"
        "    assert Path.cwd().name == 'work'\n",
        encoding="utf-8",
    )
    result = run_command(work, {"argv": ["pytest", "tests/", "-q"]})
    assert result.ok is True


# --------------------------------------------------- write_file 단위 검사

def test_write_file_requires_string_content(work: Path) -> None:
    with pytest.raises(ToolError) as caught:
        write_file(work, {"path": "a.txt", "content": {"not": "a string"}})
    assert caught.value.code == "BAD_ARGUMENTS"


def test_write_file_creates_parent_folders(work: Path) -> None:
    write_file(work, {"path": "새폴더/새파일.txt", "content": "안녕"})
    assert (work / "새폴더" / "새파일.txt").read_text(encoding="utf-8") == "안녕"


# ------------------------------------------------------- edit_file (2026-09-08)
#
# 이 도구는 실제 실패에서 나왔다. write_file 로 130줄짜리 파일을 고치라고 했더니
# 4B 모델이 목표한 한 줄은 맞게 바꿨으나 주석의 '#' 를 지워 SyntaxError 를 만들고
# 날짜를 자르는 ':' 를 ',' 로 바꿔 IndexError 를 심었다. 승인 화면의 diff 로
# 발견해 거절했다. 부분 교체 도구는 그 위험 자체를 없앤다.

SAMPLE = (
    "# 규칙 검사\n"
    "REQUIRED = (\"title\", \"date\")\n"
    "\n"
    "def check(fields):\n"
    "    # TODO: 목록이 어긋나 있다.\n"
    "    for name in (\"title\", \"date\"):\n"
    "        if name not in fields:\n"
    "            yield name\n"
    "\n"
    "def parse(line):\n"
    "    return line.split(\":\", 1)[1].strip()\n"
)


def test_edit_replaces_only_the_target_line(work: Path) -> None:
    """한 줄만 바뀌고 나머지는 글자 하나도 안 바뀐다."""
    target = work / "rules.py"
    target.write_text(SAMPLE, encoding="utf-8")

    edit_file(
        work,
        {
            "path": "rules.py",
            "old_string": '    for name in ("title", "date"):',
            "new_string": "    for name in REQUIRED:",
        },
    )

    result = target.read_text(encoding="utf-8")
    assert "for name in REQUIRED:" in result

    # 오늘 모델이 망가뜨린 두 곳이 그대로 살아 있는지 확인한다.
    assert "    # TODO: 목록이 어긋나 있다." in result   # '#' 가 지워지지 않았다
    assert 'line.split(":", 1)' in result               # ':' 가 ',' 로 안 바뀌었다

    # 줄 수도 같다. 파일이 잘려 나가지 않았다.
    assert len(result.splitlines()) == len(SAMPLE.splitlines())


def test_edit_refuses_when_string_not_found(work: Path) -> None:
    """찾지 못하면 아무것도 바꾸지 않는다. 조용히 망가지지 않는다."""
    target = work / "rules.py"
    target.write_text(SAMPLE, encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(ToolError) as caught:
        edit_file(
            work,
            {"path": "rules.py", "old_string": "없는 문자열입니다", "new_string": "새 내용"},
        )

    assert caught.value.code == "EDIT_NOT_FOUND"
    assert target.read_bytes() == before


def test_edit_refuses_when_string_appears_twice(work: Path) -> None:
    """여러 곳에 걸리면 어디를 고칠지 알 수 없으므로 거부한다."""
    target = work / "rules.py"
    target.write_text("같은 줄\n다른 줄\n같은 줄\n", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(ToolError) as caught:
        edit_file(work, {"path": "rules.py", "old_string": "같은 줄", "new_string": "바뀐 줄"})

    assert caught.value.code == "EDIT_AMBIGUOUS"
    assert target.read_bytes() == before


def test_edit_refuses_when_nothing_changes(work: Path) -> None:
    target = work / "rules.py"
    target.write_text(SAMPLE, encoding="utf-8")

    with pytest.raises(ToolError) as caught:
        edit_file(work, {"path": "rules.py", "old_string": "REQUIRED", "new_string": "REQUIRED"})
    assert caught.value.code == "EDIT_NO_CHANGE"


def test_edit_requires_approval(work: Path) -> None:
    """edit_file 도 승인 대상이다. 거절하면 파일이 그대로다."""
    target = work / "checker.py"
    before = target.read_bytes()

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
    outcome = make_agent(work, provider, approver=reject_all).run("고쳐줘")

    assert target.read_bytes() == before
    assert payload_of(tool_messages(outcome)[0])["code"] == "REJECTED_BY_USER"


def test_approval_wait_does_not_count_toward_task_timeout(work: Path) -> None:
    """승인 화면을 오래 보고 있어도 작업이 타임아웃되지 않는다.

    2026-09-08 A03 실행에서 647초 중 상당 부분이 사용자가 diff 를 읽는
    시간이었고 그 때문에 task_timeout 으로 죽었다. 승인을 신중히 볼수록
    작업이 실패하는 구조였다.
    """
    def slow_approve(request: ToolRequest) -> bool:
        time.sleep(0.4)      # 사용자가 diff 를 읽는 시간
        return True

    provider = FakeProvider(
        [
            ModelReply(tool_requests=[ToolRequest("c1", "write_file", WRITE_ARGS)]),
            ModelReply(text="고쳤습니다."),
        ]
    )
    # 작업 한도를 0.25초로 잡는다. 승인 대기(0.4초)를 세면 반드시 죽는다.
    agent = make_agent(
        work, provider, limits=Limits(task_timeout_seconds=0.25), approver=slow_approve
    )
    outcome = agent.run("고쳐줘")

    assert outcome.state is TaskState.COMPLETED
    assert outcome.reason == "final_answer"


def test_edit_outside_work_is_refused(work: Path) -> None:
    with pytest.raises(ToolError) as caught:
        edit_file(
            work,
            {"path": "../밖의파일.txt", "old_string": "a", "new_string": "b"},
        )
    assert caught.value.code == "PATH_OUTSIDE_WORK"
