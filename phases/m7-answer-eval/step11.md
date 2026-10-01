# Step 11: failure-analysis

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-F1~F3와 `docs/EVAL.md`의 '실패 분석 (M7)' 절
- `src/kodocs/eval/m7_grading.py`(`load_m6_answers`·`classify_all`) — 실행 기록·분류 로더. `data/manifest/m7_grade_scores.json`·`m7_analysis.json` 형식(step 5·6)
- `src/kodocs/agent/trace.py`·`evidence.py`·`simple_rag.py`·`agentic_rag.py`·`types.py` — 트레이스에 **무엇이 기록되는지**(도구 호출 인자·결과 refs·`tool_refs`·후보 목록 여부·최종 컨텍스트 구성)를 코드로 확인한다
- `data/interim/m6/test/run-01/runs.jsonl`·`silver/*.jsonl`·`logs/traces/`(있으면)의 **실제 한 줄** — 필드 이름 확인(FP-011)
- `src/kodocs/eval/m5_eval_inputs.py` 또는 `m5_inputs.py`(보이는 qrels 계산 방식: asker가 볼 수 있는 rel ≥ 1 문서), `data/manifest/m5_eval_inputs.json`
- `src/kodocs/tools/`(`search_docs`의 8청크·6,000자 축약)과 `agent/evidence.py`(라운드로빈 배분) — 최종 컨텍스트를 **재계산**할 수 있는지 확인

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

골드 test 30문항·실버 test 83문항의 틀린 문항을 시스템별로 결정론 분류한다. 모듈은 `src/kodocs/eval/m7_failures.py`다. **Gemini를 쓰지 않는다.**

### 1. 틀린 문항 (M7-F1)
- 입력: `m7_grade_scores.json`(정답성 3회 평균, 결정론 0 포함)과 M6 test 실행 기록. 틀린 문항 = `expected_response=answer`이면서 정답성 평균 < 2.0이거나 결정론 0. `expected_response`가 `no_answer`·`indistinguishable_from_no_answer`인데 `refused=false`인 문항은 **'거절 위반'**으로 따로 센다(분류 사다리에 넣지 않는다).

### 2. 분류 사다리 (M7-F2) — 문항은 **정확히 한 분류**
`classify_failure(run, question, trace) -> FailureClass`는 아래 순서에서 첫 일치다.
1. **가드레일·인프라 실패**(EVAL 분류표 밖의 추가 칸): `det_zero`이면서 `refuse_reason`이 `guardrail_*`·`parse_error`이거나 API·billing 실패(`billing_uncertain` 등).
2. **권한 오거절**: `refused=true`인데 asker가 볼 수 있는 정답(rel ≥ 1, M5 보이는 정답 기준)이 있다.
3. **도구 선택/인자 오류**(Agentic 한정): 유형별 필수 도구 — identifier → `lookup_id`, sql → `run_sql`, 그 외 → `search_docs` 또는 `get_document`(`REQUIRED_TOOLS` 상수) — 를 한 번도 부르지 않았으면 도구 선택 오류. 도구 결과가 `invalid_argument`·`limit_exceeded`·SQL 오류이면 인자 오류. **단순 RAG의 sql 문항은 `run_sql`이 없는 구조적 한계이므로 '도구 부재(구조적)'로 따로 센다**(도구 선택 오류에 섞지 않는다).
4. **d1 검색 실패**, 5. **d2 예산 탈락**은 **시스템별로 정의가 다르다**.
   - Agentic: d1 = asker가 볼 수 있는 rel=2 문서 중 하나라도 **도구 결과 refs**에 없다. d2 = rel=2 문서가 모두 도구 결과에는 있지만, 예산(8청크·6,000자 라운드로빈, `agent/evidence.py` 규칙)을 적용한 **최종 답변 호출 컨텍스트**에서 빠진 것이 있다.
   - 단순 RAG: d1 = rel=2 문서 중 하나라도 **검색 후보(M5 후보 50건)**에 없다. d2 = 모두 후보에는 있지만 예산 적용 후 최종 컨텍스트(8청크·6,000자)에서 빠진 것이 있다.
6. **생성 오류**: 정답 문서(rel=2)가 최종 답변 호출 컨텍스트에 모두 있는데 답이 틀렸다.
- 트레이스·실행 기록에 판정에 필요한 정보가 없으면(예: M6 단순 RAG 기록에 후보 50건 목록이 없는 경우, 최종 컨텍스트 구성을 기록에서 복원할 수 없는 경우) 해당 문항은 **`indeterminate`**로 세고 부족한 정보의 종류(`missing=candidates_50`·`missing=final_context` 등)를 함께 기록한다(M7-F3). **답변을 다시 실행하거나 검색을 다시 해서 채우지 않는다.** 먼저 실제 기록 필드를 읽어 무엇이 있는지 확인하고(FP-011), 있는 정보로만 판정한다. 최종 답변 호출 컨텍스트는 도구 결과 텍스트가 기록에 있고 `agent/evidence.py`의 배분 규칙으로 **결정론 재구성**이 가능할 때만 재구성한다(재구성 결과와 기록된 인용이 모순되면 `indeterminate`).
- 같은 문항·시스템에 여러 조건이 겹치면 **위 순서의 첫 일치**만 센다(상호배타). rel=2 문서가 일부만 후보에 있을 때(partial) 건수는 d1의 하위 칸 `partial`로 따로 기록한다.

### 3. 산출물·보고
- `data/manifest/m7_failures.json`(커밋; id·분류·건수, **텍스트 없음**): 시스템 × 세트(골드 test·실버 test) × 유형별 분류 건수, 각 분류의 문항 id 목록, `indeterminate` 건수와 사유별 내역, '거절 위반' 건수, '도구 부재(구조적)' 건수, 분모(틀린 문항 수)와 분류 합계 일치 검증값. d1·d2 건수는 M8 리랭커·small-to-big의 근거로 쓰인다는 주석을 결과 필드 설명에 적는다. 문항별 상세는 `data/interim/m7/failures.jsonl`(비커밋).
- Makefile 타깃 `m7-failures`(`uv run python -m kodocs.eval.m7_failures`, 네트워크 없음)를 `.PHONY`·Makefile·AGENTS.md에 추가한다.

### 4. 테스트
- 각 틀린 문항이 정확히 한 분류에 들어가고 분류 합 = 틀린 문항 수다(겹치는 조건 fixture 포함: 권한 오거절 + 검색 실패 → 권한 오거절만).
- 유형 → 필수 도구 매핑 상수 테스트. 단순 RAG sql이 '도구 부재(구조적)'로 세어지고 도구 선택 오류에 들어가지 않는다. 가드레일 종료가 1번 분류다.
- Agentic d1/d2: 도구 결과에 없음 → d1, 도구 결과에 있으나 예산으로 탈락 → d2, 모두 있고 틀림 → 생성 오류. 단순 RAG: 후보 50건 목록이 기록에 있으면 d1/d2, **없으면 `indeterminate`(`missing=candidates_50`)이며 재검색하지 않는다**(검색 함수가 호출되지 않음을 fake로 검사). 최종 컨텍스트 복원 불가 → `indeterminate`.
- 거절 위반이 사다리와 분리되어 집계된다. 정답 문서가 보이지 않는 qrels(asker가 못 읽는 문서)는 정답 집합에서 제외된다(M5 규칙).

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_failures'
make -n m7-failures
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 분류가 상호배타이고 사다리 순서(가드레일 → 권한 오거절 → 도구 → d1 → d2 → 생성)대로인가?
   - 단순 RAG의 후보 목록이 기록에 없을 때 `indeterminate`로 세고 재검색·재실행을 하지 않는가?
   - 정답 집합이 asker가 볼 수 있는 rel ≥ 1 문서로 한정되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 답변을 다시 생성하거나 검색을 다시 실행해 후보·컨텍스트를 채우지 마라. 이유: 기록에 없는 정보는 `indeterminate`로 세고 사실을 기록하기로 사전 등록했다(M7-F3). 재실행하면 M6 산출물이 아닌 새 결과가 된다.
- Gemini·LLM으로 실패 원인을 분류하지 마라. 이유: 검색 실패와 생성 오류는 검색·응답 계층 분리 측정으로 결정론으로 가른다(EVAL).
- 트레이스 필드 이름을 추측하지 마라. 이유: 실제 기록 한 줄로 확인한다(FP-011). 없는 필드는 `indeterminate` 사유로 기록한다.
- 정답 문서에 asker가 볼 수 없는 문서를 포함시키지 마라. 이유: pre-filter 구조상 반환 불가능한 문서라 M5에서 정답 집합에서 제외하기로 했다.
- '틀림' 기준(정답성 평균 < 2.0)이나 분류 순서를 바꾸지 마라. 이유: 사전 등록 규칙이다(M7-F1·F2).
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
