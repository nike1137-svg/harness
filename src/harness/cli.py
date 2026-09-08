"""사람이 쓰는 입구. (D02: CLI)

명령과 계약의 대응은 INTERFACES.md 의 'UI와 통신 매핑' 을 따른다.

    harness run "요청문"                 작업 시작
    harness run "요청문" --session <id>  세션 이어가기
    harness sessions                     저장된 세션 목록
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from .agent import Agent, TaskState
from .limits import DEFAULT_LIMITS
from .providers import OllamaProvider, OpenAICompatProvider, ToolRequest
from .session import Recorder, SessionStore
from .tools import ToolError, resolve_inside


RULE = "  " + "─" * 62


def show_write_diff(work_root: Path, arguments: dict) -> None:
    """무엇이 어떻게 바뀌는지 보여 준다.

    파일 전체를 보여 주면 어디가 바뀌었는지 눈으로 못 찾는다.
    바뀐 줄만 앞뒤 두 줄과 함께 보여 준다. (D06)
    """
    path = arguments.get("path", "")
    new_text = arguments.get("content", "")

    old_text = ""
    try:
        target = resolve_inside(work_root, path)
        if target.is_file():
            old_text = target.read_text(encoding="utf-8")
    except ToolError:
        # 경로가 허용 범위를 벗어난 경우다. 승인을 받아도 도구가 거부한다.
        print("  (경로가 작업 폴더 밖입니다 — 승인해도 실행되지 않습니다)")

    print(f"  파일: {path}" + ("" if old_text else "   (새 파일)"))
    print()

    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="현재",
            tofile="바뀔 내용",
            lineterm="",
            n=2,
        )
    )
    if not lines:
        print("  바뀌는 내용이 없습니다.")
        return

    shown = lines[:60]
    for line in shown:
        print(f"  {line}")
    if len(lines) > len(shown):
        print(f"  ... ({len(lines) - len(shown)}줄 더 있음)")


def show_edit(arguments: dict) -> None:
    """바꿀 문자열 한 쌍만 보여 준다.

    write_file 과 달리 파일 전체를 훑을 필요가 없다. 무엇이 무엇으로
    바뀌는지가 그대로 눈에 들어온다.
    """
    print(f"  파일: {arguments.get('path')}")
    print()

    old_lines = str(arguments.get("old_string", "")).splitlines() or [""]
    new_lines = str(arguments.get("new_string", "")).splitlines() or [""]

    for line in old_lines[:20]:
        print(f"  - {line}")
    if len(old_lines) > 20:
        print(f"  - ... ({len(old_lines) - 20}줄 더)")

    for line in new_lines[:20]:
        print(f"  + {line}")
    if len(new_lines) > 20:
        print(f"  + ... ({len(new_lines) - 20}줄 더)")


def make_approver(work_root: Path):
    """승인을 묻는 함수를 만든다.

    변경 전후를 보여 주려면 작업 폴더를 알아야 해서 이렇게 감싼다.
    """

    def approve(request: ToolRequest) -> bool:
        print()
        print(RULE)
        print(f"  승인이 필요합니다: {request.name}")
        print(RULE)

        if request.name == "write_file":
            show_write_diff(work_root, request.arguments)
        elif request.name == "edit_file":
            show_edit(request.arguments)
        elif request.name == "run_command":
            argv = request.arguments.get("argv")
            print(f"  실행할 명령: {json.dumps(argv, ensure_ascii=False)}")
            print(f"  실행 위치:   {work_root}")
            print("  (셸을 거치지 않고 이 목록 그대로 실행합니다)")
        else:
            for key, value in request.arguments.items():
                shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if len(shown) > 500:
                    shown = shown[:500] + f"... (총 {len(shown)}자)"
                print(f"    {key}: {shown}")

        print(RULE)
        try:
            answer = input("  진행할까요? [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    return approve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="블로그 글의 front matter 와 링크를 점검하는 하네스",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="요청 하나를 실행한다")
    run_parser.add_argument("request", help="모델에게 시킬 일")
    run_parser.add_argument("--session", help="이어갈 세션 식별자")
    run_parser.add_argument(
        "--provider",
        default="ollama",
        choices=["ollama", "vllm"],
        help="ollama = 로컬 (D04), vllm = Colab 의 OpenAI 호환 서버 (D08)",
    )
    run_parser.add_argument("--model", default=None, help="모델 이름. 생략하면 제공자 기본값")
    run_parser.add_argument(
        "--base-url",
        default=None,
        help="모델 서버 주소. vllm 일 때는 터널 주소를 반드시 준다",
    )
    run_parser.add_argument(
        "--work", default="work", help="작업 폴더 (도구가 접근할 수 있는 유일한 곳)"
    )
    run_parser.add_argument(
        "--sessions", default="sessions", help="세션·기록 저장 폴더 (작업 폴더 밖)"
    )

    sessions_parser = sub.add_parser("sessions", help="저장된 세션 목록")
    sessions_parser.add_argument("--sessions", default="sessions")

    return parser


def command_run(args: argparse.Namespace) -> int:
    work_root = Path(args.work).resolve()
    if not work_root.is_dir():
        print(f"작업 폴더가 없습니다: {work_root}", file=sys.stderr)
        return 2

    store = SessionStore(Path(args.sessions).resolve())

    # --- 세션 결정 ---------------------------------------------------
    previous = None
    if args.session:
        previous = store.load(args.session)
        if previous is None:
            # 기록이 없으면 복원했다고 말하지 않는다. (A09)
            print(f"그런 세션 기록이 없습니다: {args.session}", file=sys.stderr)
            return 2
        session_id = args.session
        print(f"[세션] {session_id} 를 이어갑니다 (앞 대화 {len(previous)}건)")
    else:
        session_id = store.new_id()
        print(f"[세션] {session_id}")

    recorder = Recorder(store.events_path(session_id), echo=True)

    # --- 제공자 고르기 (D04 / D08) --------------------------------------
    if args.provider == "vllm":
        if not args.base_url:
            print(
                "vllm 을 쓰려면 --base-url 에 터널 주소를 줘야 합니다.\n"
                '예: --provider vllm --base-url "https://무언가.trycloudflare.com"',
                file=sys.stderr,
            )
            return 2
        provider = OpenAICompatProvider(
            base_url=args.base_url,
            model=args.model or "cyankiwi/Qwen3.5-4B-AWQ-4bit",
        )
        # 주소는 화면에도 기록에도 남기지 않는다. (R06)
        print("[모델] Colab vLLM (주소는 기록하지 않음)")
    else:
        provider = OllamaProvider(
            model=args.model or "qwen3.5:2b",
            base_url=args.base_url or "http://localhost:11434",
        )
        print(f"[모델] 로컬 Ollama {provider.model}")

    agent = Agent(
        provider=provider,
        work_root=work_root,
        recorder=recorder,
        limits=DEFAULT_LIMITS,
        approver=make_approver(work_root),
    )

    outcome = agent.run(args.request, messages=previous)
    store.save(session_id, outcome.messages)

    print()
    if outcome.succeeded:
        print("[답변]")
        print(outcome.text)
    else:
        # 미완료를 완료로 보여 주지 않는다.
        print(f"[미완료] 상태 {outcome.state.value}, 이유 {outcome.reason}", file=sys.stderr)

    print()
    print(f"세션을 이어가려면:  harness run \"...\" --session {session_id}")
    return 0 if outcome.succeeded else 1


def command_sessions(args: argparse.Namespace) -> int:
    store = SessionStore(Path(args.sessions).resolve())
    ids = store.list_ids()
    if not ids:
        print("저장된 세션이 없습니다.")
        return 0
    for session_id in ids:
        messages = store.load(session_id) or []
        print(f"{session_id}  (대화 {len(messages)}건)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return command_run(args)
    if args.command == "sessions":
        return command_sessions(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
