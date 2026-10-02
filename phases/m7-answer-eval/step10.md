# Step 10: latency-run

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-P1~P4 (동시성 1·스트리밍 모드·구간·P95 판정·검색 구간 기술 통계)와 M6 판정 규칙 21
- `src/kodocs/llm/openai_chat.py` — `ManagedChat.invoke(..., stream=...)`의 **기존 스트리밍 경로**(`ttft_ms` 기록, 구조화 출력 스트림 처리, usage 기록). 이미 있으니 먼저 읽고 확인한다
- `src/kodocs/agent/types.py`(`ChatLike`·`AgentRuntime`)·`simple_rag.py`·`agentic_rag.py`·`finalize.py`·`trace.py` — 최종 답변 호출이 `chat.invoke(...)`를 부르는 곳, 트레이스 `timings`(`embed_s`·`search_s`·`ttft_ms`)
- `src/kodocs/eval/m6_runner.py`·`m6_test.py`·`m6_config.py` — 러너·런타임 구성·1회 잠금·dry-run 기록·`RERUN_REASON` 방식 재사용
- `data/manifest/m6_config.json`, `data/manifest/m6_eval_inputs.json`(`silver_test`·골드 test 문항 ID), `data/manifest/m6_test_runs.jsonl`
- `tests/llm/`·`tests/agent/`의 스트리밍·fake chat 테스트

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

단순 RAG 전체 P95 판정과 구간 지연 측정 러너를 만든다. 모듈은 `src/kodocs/eval/m7_latency.py`다. 이 step은 **llm/ → eval/ 순서로** 진행한다(한 step이 두 레이어에 걸치므로 llm 쪽을 먼저 끝내고 테스트한 뒤 eval 쪽으로 간다). 실패하면 `error`로 보고하고 llm/eval step으로 쪼갠다.

### 1. (llm/) 스트리밍 경로 확인 — 필요한 최소 수정만
- `ManagedChat`에 이미 `stream=True` 경로와 `ttft_ms`가 있다(`openai_chat.py`). 설치된 langchain-openai에서 **구조화 출력 + 스트리밍**이 실제로 동작하고 usage가 제공되는지 소스·타입으로 확인한다(FP-011). 부족한 점(예: usage 누락 시 처리, 첫 출력 토큰 시각 정의)이 있으면 **기본 경로(`stream=False`)의 요청 파라미터·모델 설정 해시·프롬프트 해시·동작이 바뀌지 않는 범위에서만** 최소 수정한다. 수정이 필요 없으면 수정하지 않고 그 사실을 최종 보고에 적는다.
- usage 없는 스트림 종료는 비용 누락 방지를 위해 실패로 처리되는지 확인하고 테스트한다. fixture는 SDK 실제 이벤트·청크 타입으로 만든다.

### 2. (eval/) 스트리밍 어댑터와 지연 러너
- `StreamingChat(inner: ChatLike)`: `invoke(...)`를 감싸 **`response_schema`가 있는 호출(= 최종 답변 호출)만 `stream=True`로 전달**하는 어댑터를 `m7_latency.py`에 둔다. `agent/` 코드는 수정하지 않는다(`finalizer_hash`가 `finalize.py` 소스에서 계산되어 M6 config 검증이 깨진다). 도구 선택 턴은 기존대로 비스트리밍이다.
- 러너: 113문항(골드 test 30 + 실버 test 83, `m6_eval_inputs.json`의 test 문항 ID, id 정렬 고정 순서)을 시스템별 1회, **동시성 1**로 실행한다. 동시성이 1이 아니면 `ConcurrencyError`로 거부한다(코드 강제, 옵션으로 못 바꾼다). 단순 RAG가 판정 대상이고 Agentic도 같은 조건으로 측정한다(목표 설정용). 예열: dev 문항 3건을 먼저 실행하고 결과에서 제외한다(임베더·연결 초기화 시간은 `init_s`로 따로 기록, 문항별 지연에 섞지 않는다 — FP-036).
- 구간 기록: 임베딩(`embed_s`)·검색(`search_s`)·TTFT(`ttft_ms`/1000, 최종 답변 호출의 첫 출력 토큰까지)·전체(`answer()` 호출 전후 wall clock, 재시도 포함). 트레이스 `timings`에 없는 값이 있으면 `None`으로 기록하고 개수를 보고한다.
- 판정(M7-P3): P95 = **nearest-rank**(정렬 후 `ceil(0.95 * n)`번째; n=113이면 108번째). 가드레일·실패(`guardrail_timeout` 등)도 지연으로 **포함**한다. 단순 RAG 전체 P95 ≤ 5.0초이면 `P95 목표 달성`, 초과이면 `P95 목표 미달`(점추정으로 판정). 부트스트랩 95% CI(10,000회, 시드 20261001)는 병기만 한다. **검색 구간(임베딩·검색)은 목표를 두지 않고 P50·P95·최댓값 기술 통계만 보고한다.** TTFT도 기술 통계다.
- 결과 manifest `data/manifest/m7_latency.json`(커밋): 시스템별 구간 통계·P95·판정·CI·실행 수·실패 수·동시성(=1)·`streaming=true`·예열 제외 수·`init_s`·실행 시각(KST)·코드 커밋·M6 config 해시·모델 설정 해시·**측정 환경 자동 수집값**(Python·패키지 버전(langgraph·langchain-openai·openai·mcp·google-genai 등 설치된 버전), OpenSearch 버전·힙 설정(로컬 OpenSearch의 읽기 전용 정보 API로 읽는다. 실패하면 `None`으로 기록하고 사유를 적는다, 호출은 `localhost`뿐), 기기·OS(`platform`), 임베딩 장치(임베더가 쓰는 device 기록 재사용)). 실제 문항별 기록은 `data/interim/m7/latency/runs.jsonl`(비커밋)이며 **완료·정지 시 그 파일의 SHA-256·행 수를 `m7_rawhash.record_fingerprint`(step 5)로 `m7_latency.json`의 `raw`에 기록한다**(M7-D1 예방, FP-053). 이후 P95·구간 통계 재계산과 결과 문서 생성은 현재 `runs.jsonl`이 기록과 같을 때만 진행한다(`verify_fingerprint`). 이 실행의 답변은 채점하지 않는다.
- 1회 잠금과 `RERUN_REASON`(사유·날짜·이전 결과 해시 누적, M7-P4). 재실행해도 첫 결과를 manifest에 보존한다. `--dry-run`: 실행 수(113 × 2 + 예열 6)·예상 비용(M6 test 평균 기반)·월 상한 대비를 출력하고 `m7_dry_runs.jsonl`에 기록. M6 config 해시 검사(M6 러너와 같은 방식)를 통과해야 실행한다.
- Makefile 타깃 `m7-latency-dry`, `m7-latency`(`RERUN_REASON` 변수 지원)를 `.PHONY`·Makefile·AGENTS.md 명령어 목록에 추가한다(`m7-latency`는 "실제 OpenAI 호출·과금, 동시성 1 단독 실행, 사용자 실행").

### 3. 테스트 (fake chat·fake clock, 네트워크 금지)
- 동시성 ≠ 1 거부. 예열 3건 제외(결과 수 = 113). 실패·가드레일이 지연에 포함된다. nearest-rank 경계: n=113 → 108번째, n=100 → 95번째, 정렬·동점 처리. 판정 문구(달성/미달, 경계 5.0초는 달성). 검색 구간에 목표·판정 문구가 없다(기술 통계만).
- `StreamingChat`이 `response_schema` 호출에만 `stream=True`를 전달하고 도구 선택 호출에는 전달하지 않는다. **기본 경로의 요청 파라미터 스냅샷·모델 설정 해시·프롬프트 해시·`finalizer_hash`가 `m6_config.json` 값과 같다.**
- **원본 해시**: 실행 후 `runs.jsonl`의 sha256·행 수가 `m7_latency.json`의 `raw`에 기록되고, 파일을 수정하면 재계산·보고가 거부된다.
- 환경 필드 완비(없으면 `None`+사유). 1회 잠금·재실행 사유 누적.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval tests/llm -k 'm7_latency or stream'
uv run python -m kodocs.eval.m7_latency --dry-run   # 네트워크 없음
uv run python -c "from kodocs.agent.finalize import finalizer_hash; import json; assert finalizer_hash()==json.load(open('data/manifest/m6_config.json'))['finalizer_hash']"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - `src/kodocs/agent/`에 변경이 없고 `finalizer_hash`가 M6 config 값과 같은가?
   - 동시성 1이 코드로 강제되고(옵션으로 못 바꿈) 예열 3건이 제외되는가?
   - 검색 구간에 목표·판정이 없고 기술 통계만 있는가? 전체 P95만 5.0초로 판정하는가?
   - 결과에 `streaming=true`와 측정 환경 필드가 기록되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make m7-latency`를 실행하지 마라. 이유: 실제 OpenAI 호출·과금이며 다른 부하가 없는 상태의 동시성 1 단독 실행은 사용자가 한다(M7-P1). 이 step은 `--dry-run`·fake만 쓴다.
- `agent/`(특히 `finalize.py`·`simple_rag.py`·`agentic_rag.py`·`prompts.py`·`guardrails.py`)를 수정하지 마라. 이유: `finalizer_hash`·프롬프트·가드레일 해시가 M6 config에 고정되어 변경하면 M6 검증이 깨지고 측정 대상 시스템이 바뀐다. 스트리밍은 `ChatLike` 어댑터로 주입한다.
- 검색 구간(임베딩·검색)에 목표값이나 달성/미달 문구를 넣지 마라. 이유: M6 동시성 4에서 검색 P95를 이미 봤으므로 지금 정한 목표는 결과를 보고 정한 목표가 된다. 기술 통계만 보고한다(M7-P3).
- 스트리밍 기본값을 켜지 마라. 이유: M6 test·dev 결과와 해시는 비스트리밍 호출로 만들어졌다. 스트리밍은 지연 측정 실행에서만 쓰며 결과에 조건을 적는다.
- 모델 로딩·연결 초기화 시간을 문항별 지연에 섞지 마라. 이유: 첫 문항 지연이 부풀어 P95가 왜곡된다(FP-036). `init_s`로 분리한다.
- 지연 실행 `runs.jsonl`의 원본 해시·행 수를 manifest에 남기지 않고 끝내지 마라. 이유: 원본이 비커밋이라 분석 시점에 변조·손상을 탐지할 수 없다(FP-053).
- 지연 실행의 답변을 채점·재사용하거나 지연 판정을 위해 실패 문항을 빼지 마라. 이유: 실패도 지연으로 포함하는 것이 사전 등록 규칙이다(M7-P3).
- 실제 LLM·외부 API(OpenAI·Gemini·Anthropic)를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. 이유: 비밀 값이 들어 있다.
- `data/golden/`(질의셋·qrels·사람 채점)를 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다. (`data/golden/m7_human/`·`data/golden/m7_leak_review/`는 사람만 작성하는 파일이다. 테스트 fixture는 tmp 경로에서만 만든다.)
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id·점수만 남기고 조문·표·답변·참조 답변 원문 텍스트를 넣지 마라.
- M5·M6 확정 산출물(`data/manifest/m5_*`, `data/manifest/m6_*`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5·M6 판정 규칙·기존 변경 기록을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026). (step 0이 명시한 M6 결과 문서의 지정 칸만 예외.)
- `src/kodocs/retrieval/`·`src/kodocs/tools/`의 동작을 바꾸지 마라. 이유: 권한 pre-filter와 도구 계약은 측정 대상이며 M5·M6 결과와 계약 스냅샷이 이 코드에 묶여 있다.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 지시에 없는 네트워크 요청·다운로드·패키지 설치(`uv add` 포함)를 하지 마라. 이유: 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- 기존 테스트를 깨뜨리지 마라.
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
