# 인터페이스 계약
상태: D01~D08 확정 후 채움 (2026-09-08). provider 연결부는 로컬 Ollama 0.33.3 / `qwen3.5:2b`에 실제로 요청을 보내 확인한 값이다. API는 프로그램 사이의 약속이며 HTTP만 뜻하지 않는다.

## 제품의 작업 계약
| 동작 | 입력에서 정할 것 | 출력에서 정할 것 | 오류·경계 |
|---|---|---|---|
| 작업 시작 | 요청, 세션 선택, 작업 폴더 식별 | 작업 식별자와 최초 상태 | 빈 요청, 없는 세션, 실행 중 중복 요청 |
| 상태 조회 | 작업 식별자 | 상태, 진행 내역, 결과 또는 오류 | 없는 작업, 다른 세션의 작업 |
| 변경 승인/거절 | 작업·승인 식별자, 표시한 변경에 대한 선택 | 결정 반영 결과 | 중복/만료 승인, 승인 후 변경 내용 바뀜 |
| 결과 조회 | 작업 식별자 | 결과, 근거, 변경·테스트 기록 | 미완료를 완료로 보여 주지 않음 |
| 세션 이어가기 | 세션 식별자, 새 요청 | 연결된 새 작업 | 재시작 후 기록이 없으면 복원했다고 말하지 않음 |

### 작업 상태와 허용 전이 (확정)
```
queued → running → completed
                 → failed
         running ⇄ waiting_approval
```
- `queued` 아직 시작 전. `running` 모델 호출 또는 도구 실행 중.
- `waiting_approval` 승인 대기. 이 상태에서는 파일을 바꾸거나 명령을 실행하지 않는다.
- `completed` 모델이 도구 요청 없이 최종 답을 냈다. `failed` 한도 초과·연결 실패·검사 거부로 끝났다.
- `cancelled`는 쓰지 않는다. 취소 기능은 D09에서 미뤘다.
- **거절은 상태 전이가 아니다.** D06에서 "거절 후 계속"을 택했으므로 거절하면 `waiting_approval → running`으로 돌아가고, 거절 사실이 도구 결과로 모델에게 전달된다. 작업은 `failed`가 되지 않는다.

승인은 특정 작업의 특정 도구 입력/변경 내용에 연결한다. 사용자가 본 내용이 바뀌면 그 승인을 재사용하지 않는다.

### 구체적 계약 한 개
- 이름: `read_file`
- 입력 타입과 허용 범위: `path` 문자열 하나. 작업 폴더 `work/` 기준 상대 경로. 절대 경로·`..`·심볼릭 링크로 `work/` 밖을 가리키면 거부한다. 판정은 경로를 정규화한 뒤 `work/`의 실제 경로로 시작하는지로 한다.
- 정상 입력 예: `{"path": "posts/2026-01-01-ok.md"}`
- 정상 출력 예: `{"ok": true, "path": "posts/2026-01-01-ok.md", "text": "---\ntitle: ...", "truncated": false}`
- 잘못된 입력과 오류 출력 예:
  - 폴더 밖: `{"ok": false, "code": "PATH_OUTSIDE_WORK", "message": "작업 폴더 밖의 경로입니다: /etc/passwd"}`
  - 없는 파일: `{"ok": false, "code": "FILE_NOT_FOUND", "message": "파일을 찾지 못했습니다: posts/missing.md"}`
  - 인자 누락: `{"ok": false, "code": "BAD_ARGUMENTS", "message": "path가 필요합니다"}`
- 상태 변화/부작용: 없다. 파일을 바꾸지 않는다. 실행 기록에 도구 이름·인자·성공 여부·읽은 바이트 수가 남는다.
- 재시도·중복 요청 정책: 같은 인자로 다시 불러도 결과가 같다. 같은 작업 안에서 동일 `(도구, 인자)` 호출이 반복되면 반복 한도 계산에 포함한다.

같은 틀을 나머지 도구에도 적용한다. 오류는 예외로 던지지 않고 위와 같은 구조로 돌려주어, 모델이 무엇이 잘못됐는지 읽고 다음 행동을 고를 수 있게 한다. API의 모든 필드를 모델의 자연어 판단에 맡기지 않는다.

## UI와 통신 매핑
- 선택한 방식: **CLI** (D02). 웹 서버를 붙이지 않는다.
- 명령과 계약의 대응:

| 명령 | 대응하는 계약 |
|---|---|
| `harness run "요청문"` | 작업 시작 (새 세션) |
| `harness run "요청문" --session <id>` | 세션 이어가기 |
| `harness sessions` | 저장된 세션 목록 |
| `harness show <task-id>` | 결과 조회 |

- 승인은 별도 명령이 아니라 실행 중 프롬프트로 받는다. 변경 승인/거절 계약은 실행 흐름 안에서 처리한다.
- 진행 결과를 보여 줄 방식: 한 줄씩 표준 출력에 쓴다. 도구를 부를 때 `[도구] read_file posts/... `, 결과가 오면 성공/실패와 요약, 승인이 필요하면 변경 내용을 보여 주고 `[y/n]`을 묻는다. 종료 시 상태와 이유를 한 줄로 남긴다.

## 모델 제공자 연결부
### 내부 계약
하네스 안에서는 제공자와 무관한 형태로 주고받는다.
- 하네스 → provider: 대화 목록과 도구 정의 목록.
- provider → 하네스: `ModelReply`. 최종 텍스트이거나, `ToolRequest` 목록이다.
- `ToolRequest = { id, name, arguments(딕셔너리) }`.

### 첫 제공자 (D04)
- 제공자/모델: 로컬 Ollama 0.33.3, `qwen3.5:2b` (2.3B, 컨텍스트 262144, tools 지원)
- 사용 API: `POST http://localhost:11434/api/chat` — 공식 문서 <https://github.com/ollama/ollama/blob/main/docs/api.md>
- 요청 본문: `{ model, messages, tools, stream: false, think: false }`
- 도구 정의 형식: `{"type": "function", "function": {"name", "description", "parameters"}}` — JSON Schema를 쓴다.

**2026-09-08 실측 응답** (요약):
```json
{ "message": { "role": "assistant", "content": "",
    "tool_calls": [ { "id": "call_205dy5gm",
        "function": { "index": 0, "name": "read_file",
                      "arguments": { "path": "posts/2026-01-01-ok.md" } } } ] },
  "done": true, "done_reason": "stop" }
```

- 도구 호출·결과 연결에 필요한 식별자 처리: Ollama 0.33.3은 `tool_calls[].id`를 준다. 그 값을 그대로 `ToolRequest.id`로 쓰되, **없을 때를 대비해 하네스가 자체 식별자를 만들어 채운다.** 버전에 따라 달라질 수 있으므로 어댑터에서 판정한다.
- **제공자 간 차이 (어댑터가 흡수할 것):** OpenAI 호환 API는 `arguments`를 **JSON 문자열**로 주고, Ollama는 **파싱된 객체**로 준다. 내부 계약은 딕셔너리로 통일하고, 문자열이면 어댑터에서 파싱한다. 파싱에 실패하면 도구를 실행하지 않고 `BAD_ARGUMENTS` 오류를 모델에 돌려준다.
- 도구 결과를 돌려주는 형식: 대화에 assistant 메시지(`tool_calls` 포함)를 그대로 다시 넣고, 이어서 `{"role": "tool", "tool_name": "<도구이름>", "content": "<결과 문자열>"}`을 넣는다. 실측에서 이 형식으로 모델이 원문에 맞는 답을 냈다.
- 일반 답과 도구 호출의 처리 순서: `tool_calls`가 있으면 도구 요청으로 본다. 목록에 여러 개가 오면 **받은 순서대로 하나씩** 검사·실행하고, 각 결과를 대화에 넣은 뒤 다음 모델 호출로 넘어간다. 동시에 실행하지 않는다.
- 지원하지 않는 기능·파싱 실패·네트워크 오류: 응답이 JSON이 아니거나 `message`가 없으면 실행하지 않고 작업을 `failed`로 끝낸다. 연결 실패(서버 미동작 포함)는 인증 오류와 구분해 메시지에 남긴다. **어떤 경우에도 성공으로 기록하지 않는다.**
- 제한 횟수/시간과 재시도 대상 (초기값, 실측 기준):
  - 도구 요청 총 **20회**. 초과하면 `failed`, 이유는 `tool_limit`.
  - 작업 전체 **600초**. 로컬 CPU 추론이 한 번에 20~30초 걸리므로 강의 예시(300초)보다 늘려 잡았다.
  - 도구 하나 실행 **30초**.
  - 도구 출력 **16000자**를 넘으면 자르고 `truncated: true`를 표시한다.
  - 재시도는 네트워크 오류에만 1회. 모델의 잘못된 인자는 재시도하지 않고 오류를 돌려주어 모델이 고치게 한다.

**참고 실측치 (`qwen3.5:2b`, i5-10210U, GPU 없음):** 첫 호출은 모델 적재에 약 51초가 더 걸린다. 적재 후에는 프롬프트 처리 약 29토큰/초, 생성 약 5토큰/초. 도구 왕복 한 번에 20~30초. 프롬프트 캐시가 동작해 같은 대화를 이어갈 때 앞부분은 다시 계산하지 않는다.

OpenAI와 Ollama에 같은 필드를 보내면 언제나 같은 기능이 동작한다고 가정하지 않는다. 모델 응답은 실행 전에 구조와 인자를 검사한다.

### 추가 제공자 (D08)
Colab에서 vLLM으로 띄운 `cyankiwi/Qwen3.5-4B-AWQ-4bit`. `POST <터널주소>/v1/chat/completions` (OpenAI 호환). `--enable-auto-tool-choice --tool-call-parser qwen3_coder`로 도구 호출을 켠 상태다. 이 어댑터에서 `arguments` 문자열 파싱이 실제로 필요해진다. 터널 주소와 키는 실행 기록에 남기지 않는다.

## 실행 도구 계약
| 도구 | 인자 → 반환 | 허용 범위 | 승인·오류 |
|---|---|---|---|
| `read_file` | `{path}` → `{ok, path, text, truncated}` | `work/` 안쪽 | 승인 불필요. `PATH_OUTSIDE_WORK`, `FILE_NOT_FOUND`, `BAD_ARGUMENTS` |
| `list_files` | `{path}` → `{ok, path, entries[]}` | `work/` 안쪽 | 승인 불필요. `PATH_OUTSIDE_WORK`, `DIR_NOT_FOUND` |
| `write_file` | `{path, content}` → `{ok, path, bytes_written}` | `work/` 안쪽 | **승인 필요.** 변경 전후 대조를 보여 준다. `PATH_OUTSIDE_WORK`, `REJECTED_BY_USER`, `DUPLICATE_REJECTED` |
| `run_command` | `{argv[]}` → `{ok, exit_code, stdout, stderr, timed_out}` | 허용 목록에 있는 실행 파일만, 작업 디렉터리 `work/` 고정 | **승인 필요.** 실행할 목록 전체를 보여 준다. `COMMAND_NOT_ALLOWED`, `REJECTED_BY_USER`, `TIMEOUT` |

- `run_command`의 `argv`는 **문자열이 아니라 목록**이다. 셸을 거치지 않고 실행하므로 `;`·`|`·`&&`는 해석되지 않고 인자 글자로 남는다. 허용 목록은 `pytest`로 시작한다.
- 거절된 `(도구 이름, 정규화한 인자)` 조합을 작업 단위로 기록한다. 같은 조합이 다시 오면 사용자에게 묻지 않고 `DUPLICATE_REJECTED`로 돌려주며, 차단 사실을 모델에게 알린다.
- 실행 도구 내부의 파일·명령 검사는 모델의 지시와 무관하게 적용한다. 임의 셸 명령을 허용하는 것은 별도의 권한 확대다.
