# Step 0: m6-report-fix

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/2026-10-01-m6-agent-mcp.md` — 생성 영역(`<!-- BEGIN generated -->`~`END generated`)과 보존 영역 구분, '측정 환경 기록(FP-044, 사용자 보존 영역)' 표
- `src/kodocs/eval/m6_report.py` — 입력 요약 표 생성부(`MISSING = "미실행"`, `("H5 풀", "h5_pool")` 행 정의 근처)와 그 테스트(`tests/eval/`의 m6_report 테스트)
- `src/kodocs/eval/m6_inputs.py`, `data/manifest/m6_eval_inputs.json` — 섹션별 키 구조(읽기만)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

M6 결과 문서의 두 가지를 보완한다. 이 step만 M6 결과 문서의 지정 칸을 수정할 수 있다.

### 1. 입력 요약 표의 '미실행' 표시 버그 수정 (테스트 먼저)
- 증상: `docs/results/2026-10-01-m6-agent-mcp.md` 입력 요약 표에서 'H5 풀', 'H5 풀(sql 제외 보조)', '실버 test', 'H5 실버 방향 확인' 행의 골드·실버 칸이 `미실행`으로 나온다.
- 확인된 원인: `data/manifest/m6_eval_inputs.json`의 `h5_pool`·`h5_pool_ex_sql`·`silver_test`·`h5_silver_direction` 섹션에는 `by_set` 키가 없고 `by_type`·`count`·`ids`만 있다(`dev_set` 등은 `by_set`이 있다). 생성기가 `by_set`을 읽다가 값을 못 찾는다. **먼저 `m6_report.py`를 읽고 이 원인이 맞는지 확인하라.** 다르면 실제 원인을 최종 보고에 적는다.
- 수정 방침: `by_set`이 있으면 그대로 쓰고, 없으면 `ids`의 접두(`g-`는 골드, `s-`는 실버)로 집계한다. **`m6_eval_inputs.json`과 `m6_inputs.py`는 수정하지 않는다**(이 manifest는 M6 확정 산출물이며 해시가 다른 기록에 묶여 있다).
- 실패하는 테스트를 먼저 쓴다. 현재 manifest 형태의 fixture로 기대값은 다음과 같다: H5 풀 13/0, H5 풀(sql 제외 보조) 9/0, 실버 test 0/83, H5 실버 방향 확인 0/29. 기존 행(dev 20/55, 정답 값 분모 13/43, 게이트 2/6, 답 없음 2/6)은 그대로다. 데이터가 정말 없을 때(키 자체가 없음)만 `미실행`이 나와야 한다는 테스트도 둔다.
- H5 판정 표(`13문항 판정`·`9문항 보조`·`실버 test 방향 확인`)의 `채점 대기(M7)`·`미실행` 상태 문구는 M7 채점 전이라 맞는 값이므로 **바꾸지 않는다**.

### 2. 측정 환경 표 채우기 (보존 영역, 사용자가 제공한 값만)
`## 측정 환경 기록(FP-044, 사용자 보존 영역)` 표의 아래 칸만 채운다. 값은 사용자가 2026-10-01에 제공한 것이며, 칸에 "사용자 기록 2026-10-01"을 병기한다.

| 칸 | 기입할 값 |
|---|---|
| 기기·OS·CPU·메모리 | Apple M4 Pro, 메모리 48GB, macOS 27.0.1 |
| OpenSearch 힙·버전 | OpenSearch 3.8.0, 힙 2GB |
| 임베딩 CPU/GPU·BGE-M3 버전 | BGE-M3 revision 5617a9f, MPS(Apple GPU) |
| 도구 버전·패키지 버전 | langgraph 1.2.12, langchain-openai 1.6.6, openai 3.22.1, mcp 2.2.0 |

- 나머지 칸(통합 테스트 실행일·결과, OpenAI 콘솔 Usage, 원장과 콘솔 차이)은 **건드리지 않는다**. 값을 모르는 칸을 추측으로 채우지 마라.
- 문서 맨 아래 '변경 기록' 표에 행을 추가한다: 날짜 `2026-10-01`, 변경 `측정 환경 표 기입(사용자 제공 값), 입력 요약 표 골드·실버 칸 표시 수정`, 이유 `M7 step 0. 판정·수치 변경 없음`.

### 3. 생성 영역 갱신
- `make m6-report`(네트워크 없음)로 생성 영역을 다시 만든다. 보존 영역은 그대로여야 한다.
- 최종 보고에 `git diff --stat docs/results/2026-10-01-m6-agent-mcp.md` 요약을 적는다. diff는 입력 요약 표 6칸(H5 풀·sql 제외 보조·실버 test·H5 실버 방향 확인의 골드·실버)과 측정 환경 표 4칸·변경 기록 1행뿐이어야 한다. 그 밖의 변경이 있으면 원인을 적고 되돌린다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k m6_report
make m6-report
git diff --stat docs/results/2026-10-01-m6-agent-mcp.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 입력 요약 표의 H5 풀 행이 `13 | 13 | 0`(문항 수·골드·실버), 실버 test 행이 `83 | 0 | 83`으로 나오는가?
   - 생성 영역 밖의 변경이 측정 환경 4칸과 변경 기록 1행뿐인가?
   - `m6_eval_inputs.json`이 수정되지 않았는가(`git diff --quiet data/manifest/m6_eval_inputs.json`)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `data/manifest/m6_eval_inputs.json`·`m6_inputs.py`를 고쳐 표시 문제를 해결하지 마라. 이유: M6 확정 산출물이며 입력 manifest 해시가 다른 기록에 묶여 있다(표시 문제는 보고서 쪽에서 해결한다).
- H5 판정 문구나 점수를 문서에 쓰지 마라. 이유: H5는 M7 채점 후 판정한다. 이 step은 표시·환경 기록만 보완한다.
- 사용자가 제공하지 않은 환경 값(통합 테스트 결과, 콘솔 Usage 등)을 채우지 마라. 이유: 확인되지 않은 값을 기록하면 측정 환경 기록(FP-044)이 거짓이 된다.
- 생성기가 데이터 유무가 아니라 특정 키(`by_set`)의 존재로 '미실행'을 판단하게 두지 마라. 이유: 같은 표시 버그가 M7 결과 문서에서 재발한다(FP-001: 형식만 보고 성공 판정하는 것과 같은 계열의 조용한 실패).
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
