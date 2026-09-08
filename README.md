# 블로그 글 점검 하네스

블로그 글의 front matter 규칙 위반과 깨진 링크를 찾는 개인 에이전트 하네스다.
모델에게 도구를 주고, 도구 요청을 검사·실행하고, 결과를 다시 모델에게 돌려주는
반복을 직접 구현했다. 그 반복을 다른 에이전트 도구에 맡기지 않는다.

## 무엇을 하는가

```
harness run "posts 폴더의 글을 점검해줘"
   ↓
모델이 read_file / list_files 를 요청
   ↓
경로가 work/ 안쪽인지 코드가 검사
   ↓
실행 → 결과를 대화에 넣고 다시 모델 호출
   ↓
위반 목록 또는 "문제 없음"
```

코드를 고칠 때는 변경 전후를 보여 주고 승인을 받은 뒤 적용하며, 지정한 테스트를 돌려 확인한다.

## 실행

Python 3.12 와 [uv](https://docs.astral.sh/uv/) 가 필요하다.

```bash
uv sync                 # 가상환경과 의존성
uv run pytest -q        # 하네스 자체 테스트
```

### 로컬 모델로 (개발용)

[Ollama](https://ollama.com/) 가 돌고 있어야 한다. 도구 호출을 지원하는 모델이 필요하다.

```bash
ollama pull qwen3.5:2b
uv run harness run "posts/2026-01-01-ok.md 의 front matter 를 알려줘"
```

### Colab GPU 모델로 (검증·벤치마크용)

Colab에서 vLLM을 OpenAI 호환 서버로 띄우고 터널로 연결한다.

```bash
uv run harness run "..." --provider vllm --base-url "https://<터널주소>"
```

### 세션 이어가기

```bash
uv run harness sessions                          # 저장된 세션 목록
uv run harness run "아까 그 파일에서 tags만" --session <id>
```

## 폴더 구조

```
work/                     도구가 접근할 수 있는 유일한 폴더
├── posts/                점검 대상 마크다운 (정상 1 + 결함 3)
├── checker.py            규칙 검사 함수
└── tests/                checker.py 의 테스트
src/harness/
├── cli.py                명령 해석, 승인 프롬프트, 진행 출력
├── agent.py              반복 루프 · 한도 · 작업 상태
├── providers.py          Ollama · OpenAI호환 어댑터
├── tools.py              도구 4개와 경로·명령 검사
├── session.py            세션 저장·복원, 실행 기록
└── limits.py             한도 값
tests/                    하네스 자체 테스트
sessions/                 세션과 실행 기록 (git 에 넣지 않음)
```

`work/` 가 도구의 경계다. `sessions/` 를 그 밖에 둔 이유는 하네스가 자기 기록을
도구로 읽고 쓰지 못하게 하기 위해서다.

## 권한과 승인

| 도구 | 승인 | 범위 |
|---|---|---|
| `read_file` · `list_files` | 불필요 | `work/` 안쪽만 |
| `write_file` | **필요** | `work/` 안쪽만. 변경 전후 대조를 보여 준다 |
| `run_command` | **필요** | 허용 목록에 있는 실행 파일만 |

- 경로는 `..` 와 심볼릭 링크를 모두 푼 뒤에 `work/` 안쪽인지 판정한다.
- `run_command` 는 명령을 **문자열이 아니라 목록**으로 받고 셸을 거치지 않는다.
  그래서 `;` 나 `|` 는 두 번째 명령으로 갈라지지 않고 인자 글자로 남는다.
- 거절하면 파일은 그대로이고, 거절된 요청을 다시 들이밀면 묻지 않고 막는다.
- 승인 함수를 넘기지 않으면 기본값은 **거절**이다.

모델이 무엇을 요청하든 허용 여부는 코드가 정한다. 이것을 운영체제 샌드박스라고 부르지 않는다.

## 설계 문서

| 파일 | 내용 |
|---|---|
| [PRD.md](PRD.md) | 사용자 문제, 요구사항, 선택한 범위 |
| [DECISIONS.md](DECISIONS.md) | D01~D09 결정과 그 이유 |
| [INTERFACES.md](INTERFACES.md) | 도구 계약, 상태 전이, 제공자 연결부 |
| [ACCEPTANCE.md](ACCEPTANCE.md) | 판정 시나리오와 **실제 실행 증거·실패 기록** |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | 구현 순서와 단계별 결과 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 실패 증상별 확인 순서 |
| [AGENTS.md](AGENTS.md) · CLAUDE.md | 설계 협업 지침 |

무엇이 통과했고 무엇이 아직 안 됐는지는 `ACCEPTANCE.md` 에 상태로 적었다.
실행하지 않은 것은 `NOT_RUN` 으로 남기고, 실패한 시도는 원인과 조치까지 기록한다.

## 출처

[에이전트 하네스 설계 키트](https://github.com/SunCreation/agent-building-practice)에서 출발했다.
평가에 쓰는 [Python 예시와 10문항 도구](https://github.com/SunCreation/agent-building-practice/releases/download/v4.0.0/harness-lab.zip)는 별도 자료다.
