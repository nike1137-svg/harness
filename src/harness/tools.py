"""도구 구현과 권한 검사.

모델이 무엇을 요청하든 허용 여부는 이 파일이 정한다.
모델의 지시는 '요청'이고, 판정은 여기 있는 코드가 한다. (INTERFACES.md 실행 도구 계약)
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .limits import DEFAULT_LIMITS, Limits

# run_command 로 돌릴 수 있는 실행 파일. 여기 없는 이름은 실행하지 않는다.
# 목록을 늘릴 때마다 하네스가 할 수 있는 일이 늘어난다는 뜻이다. (D05)
ALLOWED_COMMANDS = {"pytest"}


class ToolError(Exception):
    """도구가 돌려줄 오류.

    예외로 터뜨려 프로그램을 멈추지 않는다. 잡아서 모델에게 돌려주면
    모델이 무엇이 잘못됐는지 읽고 다음 행동을 고를 수 있다.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolResult:
    """도구 실행 결과. ok 가 False 면 code 와 message 를 채운다."""

    ok: bool
    payload: dict[str, Any]

    def to_model(self) -> dict[str, Any]:
        """모델에게 돌려줄 형태로 만든다."""
        return {"ok": self.ok, **self.payload}


def resolve_inside(work_root: Path, raw_path: Any) -> Path:
    """work_root 안쪽을 가리키는 경로만 돌려준다.

    밖을 가리키면 ToolError 를 낸다. 판정 순서가 중요하다.

    1. 문자열인지 먼저 본다. 모델이 숫자나 목록을 보낼 수 있다.
    2. work_root 와 이어 붙인다. raw_path 가 절대 경로면
       pathlib 이 앞의 work_root 를 버리므로, 3번에서 걸린다.
    3. resolve() 로 `..` 와 심볼릭 링크를 모두 푼다.
       푼 뒤에 비교해야 `work/../../secret` 같은 경로가 잡힌다.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError("BAD_ARGUMENTS", "path 는 비어 있지 않은 문자열이어야 합니다.")

    root = work_root.resolve()
    candidate = (root / raw_path).resolve()

    if candidate != root and not candidate.is_relative_to(root):
        raise ToolError(
            "PATH_OUTSIDE_WORK",
            f"작업 폴더 밖의 경로입니다: {raw_path}",
        )
    return candidate


def _relative(work_root: Path, target: Path) -> str:
    """사용자와 모델에게 보여 줄 때는 work_root 기준 상대 경로를 쓴다.

    전체 경로를 그대로 노출하면 사용자 이름 같은 정보가 기록에 남는다.
    """
    return target.resolve().relative_to(work_root.resolve()).as_posix()


def read_file(
    work_root: Path,
    arguments: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> ToolResult:
    """work/ 안의 파일 하나를 읽는다."""
    target = resolve_inside(work_root, arguments.get("path"))

    if not target.is_file():
        raise ToolError("FILE_NOT_FOUND", f"파일을 찾지 못했습니다: {arguments.get('path')}")

    text = target.read_text(encoding="utf-8")
    truncated = len(text) > limits.max_tool_output_chars
    if truncated:
        text = text[: limits.max_tool_output_chars]

    return ToolResult(
        ok=True,
        payload={
            "path": _relative(work_root, target),
            "text": text,
            "truncated": truncated,
        },
    )


def list_files(
    work_root: Path,
    arguments: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> ToolResult:
    """work/ 안의 폴더 목록을 준다."""
    raw = arguments.get("path", ".")
    target = resolve_inside(work_root, raw)

    if not target.is_dir():
        raise ToolError("DIR_NOT_FOUND", f"폴더를 찾지 못했습니다: {raw}")

    entries = []
    for child in sorted(target.iterdir()):
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "bytes": child.stat().st_size if child.is_file() else None,
            }
        )

    return ToolResult(
        ok=True,
        payload={"path": _relative(work_root, target), "entries": entries},
    )


def write_file(
    work_root: Path,
    arguments: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> ToolResult:
    """work/ 안의 파일에 쓴다. 승인은 이 함수가 아니라 Agent 가 먼저 받는다.

    여기까지 왔다는 건 이미 승인이 났다는 뜻이다. 권한 판정과 실행을
    한 함수에 섞지 않는다.
    """
    target = resolve_inside(work_root, arguments.get("path"))

    content = arguments.get("content")
    if not isinstance(content, str):
        raise ToolError("BAD_ARGUMENTS", "content 는 문자열이어야 합니다.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return ToolResult(
        ok=True,
        payload={
            "path": _relative(work_root, target),
            "bytes_written": len(content.encode("utf-8")),
        },
    )


def edit_file(
    work_root: Path,
    arguments: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> ToolResult:
    """파일 안의 정확한 문자열 하나를 다른 문자열로 바꾼다.

    이 도구는 2026-09-08 의 실패 때문에 생겼다. write_file 로 130줄짜리
    checker.py 를 고치라고 했더니 모델이 목표한 한 줄은 맞게 바꿨으나
    주석의 `#` 를 지워 SyntaxError 를 만들고, 날짜를 자르는 `:` 를 `,` 로
    바꿔 IndexError 를 심었다. 파일 전체를 다시 쓰게 하면 그런 일이 난다.

    바꿀 문자열만 받으면 모델이 흘릴 것이 없다. 그리고 old_string 이
    파일에 정확히 없으면 **아무것도 바꾸지 않고 거부한다.** 조용히
    망가지는 것보다 거부당하는 편이 낫다.
    """
    target = resolve_inside(work_root, arguments.get("path"))

    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")

    if not isinstance(old_string, str) or not old_string:
        raise ToolError("BAD_ARGUMENTS", "old_string 은 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(new_string, str):
        raise ToolError("BAD_ARGUMENTS", "new_string 은 문자열이어야 합니다.")
    if old_string == new_string:
        raise ToolError("EDIT_NO_CHANGE", "old_string 과 new_string 이 같습니다.")

    if not target.is_file():
        raise ToolError("FILE_NOT_FOUND", f"파일을 찾지 못했습니다: {arguments.get('path')}")

    text = target.read_text(encoding="utf-8")
    found = text.count(old_string)

    if found == 0:
        raise ToolError(
            "EDIT_NOT_FOUND",
            "old_string 을 파일에서 찾지 못했습니다. "
            "read_file 로 현재 내용을 다시 확인하고 정확히 그대로 보내세요. "
            "공백과 줄바꿈도 일치해야 합니다.",
        )
    if found > 1:
        raise ToolError(
            "EDIT_AMBIGUOUS",
            f"old_string 이 {found}곳에 나타납니다. 어디를 고칠지 알 수 없습니다. "
            "앞뒤 줄을 더 붙여서 한 곳만 가리키도록 보내세요.",
        )

    target.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")

    return ToolResult(
        ok=True,
        payload={
            "path": _relative(work_root, target),
            "replaced": 1,
            "bytes_written": len(new_string.encode("utf-8")),
        },
    )


def precheck_edit(work_root: Path, arguments: dict[str, Any]) -> None:
    """승인을 묻기 전에 이 편집이 성립하는지 먼저 본다.

    2026-09-08: 모델이 따옴표를 과도하게 이스케이프해 `\\"title\\"` 처럼 보낸 탓에
    파일과 맞지 않는 old_string 이 왔다. 그런데 하네스가 승인을 먼저 받고
    나서야 검사해서, 사용자가 헛되게 승인을 두 번 눌렀다.

    파일에 없는 문자열이라는 것은 승인을 묻기 전에 이미 알 수 있다.
    물어볼 가치가 없는 요청으로 사용자를 귀찮게 하지 않는다.
    """
    target = resolve_inside(work_root, arguments.get("path"))
    old_string = arguments.get("old_string")

    if not isinstance(old_string, str) or not old_string:
        raise ToolError("BAD_ARGUMENTS", "old_string 은 비어 있지 않은 문자열이어야 합니다.")
    if not target.is_file():
        raise ToolError("FILE_NOT_FOUND", f"파일을 찾지 못했습니다: {arguments.get('path')}")

    text = target.read_text(encoding="utf-8")
    found = text.count(old_string)

    if found == 0:
        hint = ""
        # 역슬래시가 섞여 있으면 이스케이프 실수일 가능성이 높다. 짚어 준다.
        if "\\" in old_string and "\\" not in text:
            hint = (
                " old_string 에 역슬래시(\\)가 들어 있는데 파일에는 없습니다. "
                "따옴표를 이스케이프하지 말고 파일에 보이는 그대로 보내세요."
            )
        raise ToolError(
            "EDIT_NOT_FOUND",
            "old_string 을 파일에서 찾지 못했습니다. "
            "read_file 로 현재 내용을 다시 확인하고 공백과 줄바꿈까지 그대로 보내세요."
            + hint,
        )
    if found > 1:
        raise ToolError(
            "EDIT_AMBIGUOUS",
            f"old_string 이 {found}곳에 나타납니다. 앞뒤 줄을 더 붙여 한 곳만 가리키게 하세요.",
        )


def precheck_command(work_root: Path, arguments: dict[str, Any]) -> None:
    """허용 목록에 없는 명령이면 승인을 묻지 않는다.

    사용자에게 `rm -rf` 를 승인할지 물어보는 화면 자체가 나오지 않아야 한다.
    """
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ToolError("BAD_ARGUMENTS", "argv 는 비어 있지 않은 목록이어야 합니다.")
    if not all(isinstance(item, str) for item in argv):
        raise ToolError("BAD_ARGUMENTS", "argv 의 모든 항목은 문자열이어야 합니다.")
    if argv[0] not in ALLOWED_COMMANDS:
        raise ToolError(
            "COMMAND_NOT_ALLOWED",
            f"허용 목록에 없는 명령입니다: {argv[0]} "
            f"(허용: {', '.join(sorted(ALLOWED_COMMANDS))})",
        )


def precheck_write(work_root: Path, arguments: dict[str, Any]) -> None:
    """경로가 작업 폴더 밖이면 승인을 묻지 않는다."""
    resolve_inside(work_root, arguments.get("path"))
    if not isinstance(arguments.get("content"), str):
        raise ToolError("BAD_ARGUMENTS", "content 는 문자열이어야 합니다.")


def run_command(
    work_root: Path,
    arguments: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> ToolResult:
    """허용 목록에 있는 명령을 work/ 에서 실행한다.

    argv 는 문자열이 아니라 목록이다. 셸을 거치지 않으므로
    `;` 나 `|` 는 해석되지 않고 그냥 인자 글자로 남는다.
    """
    argv = arguments.get("argv")

    if not isinstance(argv, list) or not argv:
        raise ToolError("BAD_ARGUMENTS", "argv 는 비어 있지 않은 목록이어야 합니다.")
    if not all(isinstance(item, str) for item in argv):
        raise ToolError("BAD_ARGUMENTS", "argv 의 모든 항목은 문자열이어야 합니다.")

    program = argv[0]
    if program not in ALLOWED_COMMANDS:
        raise ToolError(
            "COMMAND_NOT_ALLOWED",
            f"허용 목록에 없는 명령입니다: {program} "
            f"(허용: {', '.join(sorted(ALLOWED_COMMANDS))})",
        )

    executable = shutil.which(program)
    if executable is None:
        raise ToolError("COMMAND_NOT_ALLOWED", f"{program} 을(를) 찾지 못했습니다.")

    try:
        completed = subprocess.run(
            [executable, *argv[1:]],
            cwd=work_root,          # 어떤 명령이든 work/ 안에서만 돈다
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limits.tool_timeout_seconds,
            shell=False,            # 셸을 거치지 않는다. 이게 핵심이다.
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            "TIMEOUT",
            f"{limits.tool_timeout_seconds}초 안에 끝나지 않아 중단했습니다.",
        ) from None

    limit = limits.max_tool_output_chars
    stdout = (completed.stdout or "")[:limit]
    stderr = (completed.stderr or "")[:limit]

    # ok 는 '종료 코드가 0인가' 다. 실행됐다는 뜻이 아니다.
    # 테스트가 실패했는데 ok=True 로 돌려주면 모델이 그것만 보고
    # 됐다고 할 수 있다. 그게 강의가 경계하는 실패다.
    return ToolResult(
        ok=completed.returncode == 0,
        payload={
            "argv": argv,
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
        },
    )


# 모델에게 넘길 도구 설명. JSON Schema 형식이다.
# 설명을 대충 쓰면 모델이 엉뚱한 인자를 만든다. 인자 이름과 뜻을 분명히 적는다.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "작업 폴더 안의 파일 하나를 읽어 내용을 그대로 돌려준다. "
                "블로그 글의 front matter 나 본문을 확인할 때 쓴다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "작업 폴더 기준 상대 경로. 예: posts/2026-01-01-ok.md",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "작업 폴더 안의 폴더 내용을 나열한다. "
                "어떤 글이 있는지 모를 때 먼저 부른다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "작업 폴더 기준 상대 경로. 생략하면 작업 폴더 자체. 예: posts",
                    }
                },
                "required": [],
            },
        },
    },
]

TOOL_DEFINITIONS.extend(
    [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "작업 폴더 안의 파일에 내용을 쓴다. 기존 파일은 통째로 덮어쓴다. "
                    "사용자 승인이 필요하며, 거절되면 파일은 바뀌지 않는다. "
                    "파일을 고칠 때는 먼저 read_file 로 현재 내용을 확인하라."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "작업 폴더 기준 상대 경로. 예: checker.py",
                        },
                        "content": {
                            "type": "string",
                            "description": "파일에 넣을 전체 내용. 바뀐 부분만 보내면 안 된다.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "파일 안의 정확한 문자열 하나를 다른 문자열로 바꾼다. "
                    "코드를 고칠 때는 write_file 보다 이 도구를 먼저 쓴다. "
                    "파일 전체를 다시 쓰지 않아도 되므로 다른 곳을 실수로 망가뜨리지 않는다. "
                    "old_string 은 read_file 로 본 내용과 공백까지 정확히 같아야 하고, "
                    "파일 안에서 딱 한 곳만 가리켜야 한다. 사용자 승인이 필요하다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "작업 폴더 기준 상대 경로. 예: checker.py",
                        },
                        "old_string": {
                            "type": "string",
                            "description": (
                                "바꿀 대상 문자열. 파일에 있는 그대로여야 한다. "
                                "짧아서 여러 곳에 걸리면 앞뒤 줄을 더 붙인다."
                            ),
                        },
                        "new_string": {
                            "type": "string",
                            "description": "그 자리에 넣을 새 문자열.",
                        },
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "허용된 명령을 작업 폴더에서 실행한다. 사용자 승인이 필요하다. "
                    f"허용된 실행 파일: {', '.join(sorted(ALLOWED_COMMANDS))}. "
                    "종료 코드가 0이 아니면 실패이며, 그때는 stdout 을 읽고 원인을 판단하라."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "명령과 인자를 나눠 담은 목록. "
                                '예: ["pytest", "tests/", "-q"]. '
                                "한 문장으로 붙여 쓰면 안 된다."
                            ),
                        }
                    },
                    "required": ["argv"],
                },
            },
        },
    ]
)

# 이름 -> 실행 함수. 여기 없는 이름은 실행하지 않는다.
TOOL_HANDLERS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_command": run_command,
}

# 승인을 묻기 **전에** 부르는 검사. ToolError 를 내면 사용자에게 묻지 않고
# 그 오류를 모델에게 돌려준다. 물어볼 가치가 없는 요청으로 사람을 귀찮게
# 하지 않는다. 검사가 없는 도구는 그냥 승인 절차로 간다.
TOOL_PRECHECKS = {
    "edit_file": precheck_edit,
    "write_file": precheck_write,
    "run_command": precheck_command,
}
