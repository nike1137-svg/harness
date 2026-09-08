# 하네스 구현과 실험 보고서

2026-09-08 · 블로그 글 점검 하네스

## 제품과 구현

- **사용자 시나리오 / PRD 위치**: [PRD.md](PRD.md). 매일 블로그를 쓰는 사람이 쌓인 글의 머리말(front matter) 규칙 위반과 깨진 내부 링크를 찾고, 검사 코드를 고칠 때는 변경 전후를 확인하고 승인한 뒤 테스트로 결과를 확인한다.
- **구현 언어와 실행 방법**: Python 3.12 · uv. `uv sync` 후 `uv run harness run "..."`. 자세한 실행법은 [README.md](README.md).
- **직접 작성한 부분 / 참고한 부분**: 모델·도구 반복, 권한 검사, 승인 절차, 세션, 한도, 기록을 전부 직접 작성했다. 에이전트 프레임워크를 쓰지 않았다. 퍼실 제공 `harness-lab` 은 **평가 도구로만** 사용했고 그쪽 `local_agent.py` 의 함수 계약(`async solve_task`)만 맞췄다. 코드를 가져오지 않았다.
- **코드 위치**

| 책임 | 파일 |
|---|---|
| 반복 루프 · 한도 · 작업 상태 | `src/harness/agent.py` |
| 제공자 어댑터 (Ollama · OpenAI 호환) | `src/harness/providers.py` |
| 도구 6개 · 경로/명령 검사 · 승인 전 검사 | `src/harness/tools.py` |
| 세션 저장·복원 · 실행 기록 | `src/harness/session.py` |
| 한도 값 | `src/harness/limits.py` |
| 명령 해석 · 승인 화면 | `src/harness/cli.py` |
| 평가 도구 연결부 | `src/harness/benchmark.py` |

- **Python 예시와 다른 설계 결정**

| 결정 | 내용과 이유 |
|---|---|
| `edit_file` 신설 | 파일 전체 덮어쓰기 대신 문자열 한 쌍만 교체한다. 실제로 `write_file` 로 130줄을 다시 쓰게 했더니 목표한 줄은 맞게 바꾸고 주석의 `#` 를 지워 `SyntaxError` 를 만들었다. 대상 문자열이 없으면 **아무것도 바꾸지 않고 거부**한다 |
| `run_python` 을 `run_command` 와 분리 | 허용 목록에 `python` 을 넣으면 `["python","-c","아무 코드"]` 가 통과해 셸을 막아 둔 의미가 없어진다. 작업 폴더 안의 `.py` 파일만 실행하는 별도 도구로 뒀다 |
| 승인 **전** 검사(`TOOL_PRECHECKS`) | 파일에 없는 문자열을 바꾸겠다거나 허용 목록 밖 명령을 돌리겠다는 요청은 사용자에게 묻지 않고 그 자리에서 거부한다. 물어볼 가치가 없는 요청으로 사람을 부르지 않는다 |
| 승인 대기 시간을 작업 시계에서 제외 | 사람이 변경 내용을 읽는 시간은 하네스가 일한 시간이 아니다. 빼지 않았더니 승인을 신중히 볼수록 작업이 죽었다(647초 사례) |
| `files_changed` 를 결과에 포함 | 모델이 "수정하겠습니다"라고 쓰고 도구를 부르지 않은 채 끝냈는데 상태가 `completed` 였다. 바뀐 파일 수를 함께 보여 준다 |
| 세션 기록을 작업 폴더 **밖**에 배치 | 하네스가 자기 실행 기록을 도구로 읽거나 고치지 못하게 한다. 기록을 손댈 수 있으면 그 기록으로 무엇이 있었는지 판단할 수 없다 |

## 평가 조건

- **원본 commit / subset ID / manifest SHA256**
  - 원본: `alibaba/terminal-bench-pro` @ `874af409da6aafebccbf3bc5bb41a2fa4d78784d`
  - subset: `terminal-bench-pro-local-port-v1` (port 1.0.0)
  - manifest SHA256: `9313a9bc65812d9d77b433d409f42d75ac76688a762921e5bca6530419e2964f` (기준·개선 동일)
- **제공자와 모델**: Colab T4 GPU에서 vLLM으로 띄운 `cyankiwi/Qwen3.5-4B-AWQ-4bit`. Cloudflare Tunnel로 연결. 하네스는 OpenAI 호환 어댑터(`OpenAICompatProvider`)로 붙는다.
  - `run-metadata.json` 의 `provider` 가 `ollama` 로 기록된 것은 harness-lab 의 `--provider` 가 `openai|ollama` 만 받기 때문이다. 실제 제공자는 환경 변수 `HARNESS_BASE_URL` 로 지정했고, 주소를 명령줄에 적지 않은 이유는 실행 기록에 남기지 않기 위함이다.
- **코드 SHA256**
  - `my_agent.py`(연결 껍데기): `7ebca7faa955e1df0c379fb831062b489bc8fc7f802157073d0f5b8698931891` — **기준·개선 동일**
  - 실제 하네스 코드는 이 저장소의 커밋으로 추적한다. **두 실행 사이에 코드 파일은 바뀌지 않았고, 환경 변수 `HARNESS_PROVIDER_RETRIES` 만 `0` → `2` 로 달랐다.**
- **실행 환경**: Windows-11-10.0.26200 / Python 3.13.14(평가 도구) · 3.12(하네스) / local-port 1.0.0 / Docker 미사용
- **의존성 잠금**: `uv.lock` (하네스), `harness-lab` 은 자체 `uv.lock`
- **문항 수**: 10 (easy 2 / medium 4 / hard 4), 문항당 반복 1회
- **한도** (기준·개선 동일)

| 항목 | 값 |
|---|---|
| 평가 도구 예산 | 문항당 40단계 · 300초 · 도구 10초 |
| 하네스 작업 한도 | **255초** (예산의 0.85배) |
| 하네스 도구 호출 한도 | 160회 (40 × 4) |
| 모델 호출 한도 | 남은 예산에 맞춰 매 호출 조정, 최소 15초 |
| 도구 출력 상한 | 16,000자 |

  작업 한도를 예산보다 짧게 잡은 이유는 아래 "측정이 안 됐던 두 번" 참조.

- **실행 폴더**
  - 기준: `jobs/baseline-v2` (2026-09-08 13:41:18Z → 14:18:28Z, 37분)
  - 개선: `jobs/improved-v2` (14:18:51Z → 14:56:19Z, 37분)
  - **23초 간격으로 연달아 실행**했다. 같은 서버·같은 세션에서 측정해 조건 차이를 줄였다.
- **리포트**: `reports/baseline-v2/`, `reports/comparison/` (각각 `index.html` · `report.json` · `trials.csv`)

## 결과

원본 결과는 `jobs/` 아래 `result.json` 과 `agent/events.jsonl` 에, 집계는 `reports/` 의 CSV·JSON·HTML에 있다. 실패·오류·미완료를 구분해 적었고 **분모에서 빼지 않았다.**

| 측정 | 공식 통과 | 채점기 raw_reward | 검사 통과 | easy | medium | hard | 실패 | 실행 오류 | 미완료 | 시간 | 토큰 | 비용 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 기준 | **0/10** | 0 | **28/304** | 14/35 | 8/68 | 6/201 | 1 | 9 | 9 | 37분 | 알 수 없음 | 0원 |
| 개선 | **0/10** | **1** | **61/304** | **32/35** | **23/68** | 6/201 | 1 | 9 | 9 | 37분 | 알 수 없음 | 0원 |

- **토큰**: 관측하지 않았다. vLLM 응답의 usage를 하네스가 기록하지 않는다.
- **비용**: 로컬 노트북과 Colab 무료 티어만 사용했다. 유료 API를 쓰지 않았으므로 0원이며 별도 가격표 계산도 하지 않았다.
- **공식 통과가 0/10 인 이유**: 아래 "산출물은 맞았는데 통과가 아니다" 참조.

### 문항별

| 문항 | 난도 | 기준 상태 | 기준 검사 | 개선 상태 | 개선 검사 | |
|---|---|---|---|---|---|---|
| extract-paper-metadata-to-json | easy | provider_connection | 12/13 | task_timeout | **13/13** | ▲ |
| python-sudoku-solver-backtracking | easy | provider_connection | 2/22 | task_timeout | **19/22** | ▲ |
| detect-corrupted-blockchain-transaction | medium | provider_connection | 8/10 | provider_protocol | **1/10** | ▼ |
| implement-go-board-analyzer | medium | provider_connection | 0/16 | completed | 0/16 | — |
| python-sokoban-bfs-solver | medium | provider_protocol | 0/12 | provider_connection | 0/12 | — |
| recover-encrypted-db-credentials | medium | provider_connection | 0/30 | task_timeout | **22/30** | ▲ |
| advanced-json-to-rfc4180-csv-converter | hard | provider_protocol | 0/90 | provider_connection | 0/90 | — |
| implement-depgraph-dependency-resolver | hard | completed | 0/13 | task_timeout | 0/13 | — |
| implement-lz77-file-compressor | hard | provider_connection | 2/85 | provider_connection | 2/85 | — |
| implement-nonogram-puzzle-solver | hard | provider_protocol | 4/13 | provider_connection | 4/13 | — |

### 종료 이유 분포

| 종료 이유 | 기준 | 개선 |
|---|---|---|
| `provider_connection` | 6 | 4 |
| `provider_protocol` | 3 | **1** |
| `task_timeout` | 0 | **4** |
| `completed` | 1 | 1 |
| 도구 호출 합계 | 103회 | 79회 |
| 파일을 만든 문항 | 4 | **10** |

## 실패 분석과 개선 가설

### 대표 실패 — 기준 실행의 `extract-paper-metadata-to-json`

- **요청**: 논문 텍스트 3개에서 메타데이터를 뽑아 JSON으로 만들라
- **관찰한 도구 실행**: `list_files` → `read_file` 반복 8회, 파일 1개 생성
- **채점 결과**: 13개 검사 중 **12개 통과**, `raw_reward 0`
- **종료**: `provider_connection`, 252초

**13개 중 12개까지 갔는데 모델 호출 오류 하나로 끝났다.** 그 시점에 도구를 8회 썼고 한도는 160회였다.

### 추정 원인과 뒷받침하는 기록

기준 실행 10문항 중 **9문항이 모델 호출 오류로 중단**됐다(`provider_connection` 6 + `provider_protocol` 3). 하네스는 `ProviderError` 를 받으면 **재시도 없이 즉시 작업을 끝내고** 있었다(`agent.py` 의 `except ProviderError` → `_finish`).

Cloudflare Tunnel을 거쳐 Colab의 vLLM에 붙는 구성에서 502나 일시적 연결 끊김은 **다시 시도하면 성공할 수 있는 오류**다. 그런데 한 번 걸리면 그대로 끝이었다.

### 변경한 한 가지 요소

**모델 호출이 연결·형식 오류로 실패했을 때 최대 2회까지 재시도한다** (`Limits.max_provider_retries` 를 `0` → `2`).

- 대기는 2초 → 4초로 늘린다
- **인증 오류는 재시도하지 않는다** — 다시 물어도 같은 답이 온다
- **남은 예산이 부족하면 재시도하지 않는다** — 기다렸다 호출할 시간이 없으면 그대로 끝낸다
- 재시도 사실을 `provider_retry` 로 기록에 남긴다

코드 파일은 양쪽 실행에서 동일하고, **환경 변수 `HARNESS_PROVIDER_RETRIES` 하나만** 달랐다. 회귀 테스트 5건(`tests/test_honesty.py`)으로 동작을 고정했다.

### 예상한 영향

한 번의 일시적 오류로 작업이 끝나지 않으므로 완주율과 검사 통과 수가 오른다.

### 실제 변화 — 좋아진 것과 나빠진 것

**좋아진 것**

| 문항 | 기준 → 개선 |
|---|---|
| `extract-paper-metadata-to-json` | 12/13 → **13/13**, `raw_reward 0 → 1` |
| `recover-encrypted-db-credentials` | 0/30 → **22/30** |
| `python-sudoku-solver-backtracking` | 2/22 → **19/22** |
| 검사 통과 합계 | 28/304 → **61/304** (2.2배) |
| 파일을 만든 문항 | 4 → **10** |
| easy 2문항 | 14/35 → **32/35** |
| medium 4문항 | 8/68 → **23/68** |

**나빠진 것**

| 문항 | 기준 → 개선 |
|---|---|
| `detect-corrupted-blockchain-transaction` | **8/10 → 1/10** |

이 문항은 개선 실행에서 `provider_protocol` 로 58초 만에 끝났다. 재시도가 회복시키지 못한 경우이며, 재시도가 모든 오류에 듣는 것은 아님을 보여 준다.

**변화가 없는 것**

hard 4문항은 6/201 그대로였다. LZ77 압축기·논오그램 솔버 같은 문제는 오류를 넘겨도 300초 안에 만들 수 없다. **이 개선은 "거의 다 했는데 끊긴" 문항에만 듣는다.**

### 종료 이유가 이동했다

```
기준  : provider_connection 6 · provider_protocol 3 · completed 1
개선  : provider_connection 4 · task_timeout 4 · provider_protocol 1 · completed 1
```

`task_timeout` 이 0건에서 4건으로 늘었다. **오류로 즉시 끝나던 것이 계속 일하다가 시간이 다 되는 쪽으로 바뀌었다.** 실패의 성격이 "중단"에서 "시간 부족"으로 옮겨갔고, 그만큼 더 많은 검사를 통과했다.

### 산출물은 맞았는데 통과가 아니다

개선 실행의 `extract-paper-metadata-to-json` 은 이렇게 끝났다.

```
raw_reward : 1        채점기는 통과로 판정
checks     : 13/13    산출물이 요구를 모두 만족
status     : error (AgentIncomplete, task_timeout)
공식 집계  : pass 아님
```

**요구된 파일을 다 만들어 놓고, 완료를 알리기 전에 시간이 다 됐다.** 평가 도구는 미완료를 통과로 세지 않으므로 공식 점수는 0/10 그대로다. 이 판정은 옳고, 분모에서 빼지 않았다.

### 동일하게 유지한 조건 / 바뀐 조건

| 유지 | 바뀜 |
|---|---|
| 모델(`Qwen3.5-4B-AWQ-4bit`), 서버 인스턴스, manifest(SHA256 동일), 문항 10개, 반복 1회, 예산(40단계·300초·10초), 하네스 코드 파일, `my_agent.py` SHA256 | `HARNESS_PROVIDER_RETRIES` 0 → 2 |

두 실행은 23초 간격으로 연달아 돌려 서버 상태 변화를 줄였다.

### 결론과 다음 실험

**가설은 부분적으로 맞았다.** 재시도가 일시적 오류를 넘겨 검사 통과를 2.2배로 늘렸지만, **공식 통과 수는 0/10에서 움직이지 않았다.** 한 문항은 산출물을 완성하고도 완료 신호를 내지 못했다.

**병목이 이동했다.** 서버 오류(9건 → 5건)에서 **시간 예산**(`task_timeout` 0건 → 4건)으로. 다음 실험은 여기를 건드려야 한다.

다음 가설 후보 — 이번 기록에서 나온 것들이다.

1. **작업을 끝내고 완료를 알리는 데 쓸 시간을 남긴다.** 예산의 마지막 구간에서는 새 도구 호출을 시작하지 않고 정리하게 한다. `extract-paper` 가 13/13을 해놓고 미완료로 끝난 사례가 직접 근거다.
2. **도구 결과 출력을 더 줄인다.** 문항당 도구 호출이 최대 23회였고 대화가 길어질수록 모델 호출이 느려졌다. 출력 상한(16,000자)이 시간 예산을 갉아먹는지 확인한다.
3. **경로 탐색을 줄인다.** 기준 실행에서 `DIR_NOT_FOUND` 11회 · `FILE_NOT_FOUND` 8회가 나왔고 같은 경로를 반복해서 물었다. 경로를 못 찾았다는 오류에 **그 자리에 실제로 무엇이 있는지** 함께 돌려주면 추측 반복이 줄어들 수 있다.

**한계** — 이 결과를 일반화하지 않는다.

- 문항당 1회 실행이라 **우연을 배제할 수 없다.** 반복 3회로 재측정해야 한다.
- 같은 10문항을 개선에 반복 사용한 **개발 실험**이며, 그 문항에 맞춰 조정될 위험이 있다.
- 4B 양자화 모델 한 종류에서만 확인했다. 큰 모델에서도 같은 효과가 나는지는 알 수 없다.
- 교육용 로컬 이식판이며 **공식 컨테이너 점수가 아니다.**

## 측정이 안 됐던 두 번 — 계측 자체를 고친 기록

정식 측정 전에 두 번의 실행이 **데이터를 남기지 못했다.** 개선 실험의 일부가 아니라 계측 장비를 고친 과정이며, 조건을 바꾼 것이 아니므로 기준값으로 쓰지 않았다.

**1차 (`jobs/baseline`, 로컬 `qwen3.5:2b`)** — 10문항 중 **9개가 `agent_result: null`**. 하네스 작업 한도와 평가 도구 한도를 똑같이 300초로 잡아 경합했고, 저쪽이 먼저 `TimeoutError` 를 던져 이쪽 결과가 버려졌다. 채점도 돌지 않았다.

**2차 (`jobs/baseline2`)** — 작업 한도를 255초로 줄였는데 1문항째부터 같은 증상. 한도 검사가 **모델 호출이 끝난 뒤에만** 이뤄지므로, 예산이 얼마 안 남은 상태에서 시작한 호출이 예산을 넘겼다.

**조치** — 모델을 부르기 직전에 남은 예산만큼으로 대기 시간을 좁혔다(`agent.py`). 끊더라도 하네스가 끊어야 종료 이유와 사용량을 남길 수 있다.

**교훈** — 측정하려는 시스템이 측정 도구보다 늦게 끝나면 아무것도 기록되지 않는다. 기준·개선 두 실행 모두 이 수정이 적용된 상태에서 이뤄졌다.

## 제출 확인

- [x] 실행 가능한 소스와 잠금 파일, 실행 안내 — `src/`, `tests/`, `uv.lock`, [README.md](README.md)
- [x] PRD·결정 기록·인터페이스·완료 조건 — [PRD.md](PRD.md) · [DECISIONS.md](DECISIONS.md) · [INTERFACES.md](INTERFACES.md) · [ACCEPTANCE.md](ACCEPTANCE.md)
- [x] 기준/개선의 설정, `run-metadata.json`, 원본 trial result와 실행 기록 — `jobs/baseline-v2`, `jobs/improved-v2`
- [x] 두 실행의 10문항 결과 CSV·JSON·HTML — `reports/baseline-v2/`, `reports/comparison/`
- [x] 개선 가설, 구현 변경과 관찰 결과 — 이 문서
- [x] 키·토큰·개인 자료 제외 — API 키를 쓰지 않았고, 터널 주소는 환경 변수로만 전달해 기록에 남기지 않았다

**제출 묶음에서 제외한 것** — 원본 문제·정답·채점 코드는 재배포하지 않는다. `jobs/*/source/benchmark/upstream`, `.benchmark-cache`, 각 trial의 `workspace/`(문제 자료 사본)와 `verifier/` 는 제외했다. 다시 받으려면 고정 manifest로 준비 명령을 실행한다.

```
uv run python -m harness_lab.benchmark_source --prepare
```

## 로컬 이식과 검증 범위

- **난이도 출처**: 고정 upstream `task.toml` (easy 2 / medium 4 / hard 4)
- **실행 방식**: local-port, Docker 미사용
- **OS·CPU·동시 실행**: Windows 11 (26200), 8코어. 두 실행 모두 모델은 Colab GPU에서 돌았고 로컬 CPU는 평가 도구와 채점기만 사용했다. 브라우저 등 일상 프로그램이 함께 떠 있었다.
- **원본 대비 변경**: 작업 경로를 시행별 폴더로 이동(`/app → app`, `/protected → protected` 등), 현재 Python 실행 파일 사용, 설치 보일러플레이트 제외, 기대값 파일의 평가자 전용 이동
- **채점기 제약**: GoBoard 검증기는 `mypy` 와 `flake8` 실행 파일을 요구한다. 원본 테스트가 두 도구의 실패 종료 코드를 assert하지 않으므로 통과가 lint/typecheck 성공을 보장하지 않는다. 이 문항은 양쪽 실행 모두 0/16이었다.
- **다른 운영체제에서 실행했는지**: 아니다. Windows에서만 확인했다.

공식 컨테이너 점수나 전체 벤치마크 점수로 표현하지 않는다. 이 10문항에서 관찰한 차이를 전체 성능 우월성으로 일반화하지 않는다.
