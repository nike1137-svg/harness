# 벤치마크 실행 증거 (A11·A12)

고정 10문항 평가의 **기준 실행과 개선 실행 원본 결과**다.
분석과 결론은 [../EXPERIMENT_REPORT.md](../EXPERIMENT_REPORT.md) 에 있다.

## 무엇이 들어 있나

```
jobs/baseline-v2/          기준 실행 (재시도 없음)
jobs/improved-v2/          개선 실행 (재시도 2회)
  ├── run-metadata.json        모델·한도·manifest SHA256·시작/종료 시각
  ├── requested-config.json    실행할 때 준 설정
  └── <문항>__1/
      ├── result.json          에이전트 상태·지표·채점 결과
      └── agent/
          ├── events.jsonl     도구 요청과 결과가 일어난 순서대로
          └── sessions/trial.json   모델에게 실제로 전달된 대화

reports/baseline-v2/       기준 집계 (index.html · report.json · trials.csv)
reports/comparison/        개선 집계와 기준 대비 비교
my_agent.py                평가 도구가 부른 연결 껍데기
tasks-manifest.json        문항 목록과 원본 파일 SHA256
```

## 무엇을 뺐나

**원본 문제·정답·채점 코드는 재배포하지 않는다.** 다음을 제외했다.

| 뺀 것 | 이유 |
|---|---|
| `jobs/*/source/` | 평가 도구와 upstream 원문 사본 |
| `jobs/*/<문항>/workspace/` | 문항별 작업 폴더 — 원본 fixture 사본이 들어 있다 |
| `jobs/*/<문항>/verifier/` | 채점기 출력 |
| `.benchmark-cache/` | 원본 다운로드 캐시 |

집계에 필요한 수치는 `result.json` 과 `reports/` 에 모두 남아 있다.

## 다시 받는 방법

문항은 고정 commit에서 받고 SHA256으로 검증한다.

```
uv run python -m harness_lab.benchmark_source --prepare
```

- 원본: `alibaba/terminal-bench-pro` @ `874af409da6aafebccbf3bc5bb41a2fa4d78784d`
- subset: `terminal-bench-pro-local-port-v1` (port 1.0.0)
- manifest SHA256: `9313a9bc65812d9d77b433d409f42d75ac76688a762921e5bca6530419e2964f`

## 두 실행을 재현하려면

`my_agent.py` 를 `harness-lab` 루트에 두고, 환경 변수 `MY_HARNESS_SRC` 로 이 저장소의 `src` 경로를 알려 준다.

```bash
# 기준 — 재시도 없음
HARNESS_BASE_URL="<모델 서버 주소>" \
  uv run python -m harness_lab.bench --name baseline-v2 \
  --agent my_agent:solve_task --provider ollama --model cyankiwi/Qwen3.5-4B-AWQ-4bit

# 개선 — 재시도 2회. 바뀌는 것은 이 환경 변수 하나뿐이다
HARNESS_BASE_URL="<모델 서버 주소>" HARNESS_PROVIDER_RETRIES=2 \
  uv run python -m harness_lab.bench --name improved-v2 \
  --agent my_agent:solve_task --provider ollama --model cyankiwi/Qwen3.5-4B-AWQ-4bit
```

모델 서버 주소를 환경 변수로 받는 이유는 명령줄에 적으면 실행 기록과 화면에 남기 때문이다.
`--provider ollama` 로 적히는 것은 평가 도구가 `openai|ollama` 만 받기 때문이며,
`HARNESS_BASE_URL` 이 있으면 OpenAI 호환 어댑터가 쓰인다.

## 결과 요약

| | 기준 | 개선 |
|---|---|---|
| 공식 통과 | 0/10 | 0/10 |
| 검사 통과 | 28/304 | **61/304** |
| 파일을 만든 문항 | 4 | **10** |
| 서버 오류로 중단 | 9건 | **5건** |

**공식 통과는 움직이지 않았다.** 그 아래 지표가 왜 달라졌는지, 나빠진 문항은 무엇인지,
어떤 한계가 남는지는 [../EXPERIMENT_REPORT.md](../EXPERIMENT_REPORT.md) 에 적었다.
