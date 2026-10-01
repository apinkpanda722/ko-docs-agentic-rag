# Step 13: m7-report

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/2026-10-01-m6-agent-mcp.md`와 `src/kodocs/eval/m6_report.py` — 결과 문서 구조(생성 영역 `<!-- BEGIN generated -->`~`END generated`, 사용자 보존 영역, `MISSING`/'미실행' 처리), step 0에서 고친 표시 버그
- `docs/results/2026-09-30-m5-retrieval.md`(검색 사다리·H1~H4·H7 결과)와 `docs/EVAL.md`의 '결과 보고' 규칙·`docs/PRD.md`의 '성공 기준'
- `data/manifest/`의 `m7_probe.json`·`m7_pilot.json`·`m7_human_sample.json`·`m7_human_grades.json`·`m7_config.json`·`m7_grade_scores.json`·`m7_grade_summary.json`·`m7_analysis.json`·`m7_security_set.json`·`m7_security_results.json`·`m7_leak_review.json`·`m7_leak_facts.json`·`m7_latency.json`·`m7_failures.json`·`m7_dry_runs.jsonl`(없는 파일은 '미실행'으로 다룬다) — 각 생성 step의 파일 형식
- `data/manifest/costs.jsonl`·`src/kodocs/costs.py`(원장 읽기), `data/interim/m6/`·`data/manifest/m6_test_results/`(운영 지표 재집계 입력)
- `src/kodocs/eval/m7_stats.py`·`m7_analyze.py`·`m7_security.py`·`m7_latency.py`·`m7_failures.py` — 필드 이름 확인
- `docs/THREAT_MODEL.md`(step 12), `docs/results/hypotheses.md`의 'M7 판정 규칙'

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

M7 최종 결과 문서 생성기와 운영 지표 집계를 만든다. 모듈은 `src/kodocs/eval/m7_report.py`다.

### 1. 결과 문서 틀
- 파일명: `docs/results/{실행일}-m7-answer-eval.md`. **실행일은 이 step을 실행하는 날(KST, `date +%Y-%m-%d`로 확인)이며 파일명 날짜를 `RESULT_PATH` 상수로 고정한다**(M6의 고정 파일 방식). 실행일과 관계없이 이후 재생성은 같은 파일을 갱신한다.
- 구조는 M6 결과 문서와 같다: `<!-- BEGIN generated -->`~`<!-- END generated -->` 사이는 생성기가 manifest에서 만들고, 그 밖의 절은 보존한다(초기 문서는 생성기가 틀을 만들어 쓰고 이후에는 생성 영역만 갱신). 데이터가 없는 칸만 `미실행`이다.
- 생성 영역에 싣는 것: 실행 정보(고정 날짜·생성일·생성 코드 커밋·측정 코드 커밋·M5/M6 config 해시·`m7_config` 해시), **PRD 성공 기준 6개 체크표**(검색 실험표(M5 결과 문서 링크, 결과 수치 재계산 금지)·사전 등록 가설 채택/기각·단순 대 Agentic 유형별 비교와 실패 원인·보안 누설률 0%·κ ≥ 0.6·단순 RAG P95 ≤ 5s — 각 항목의 상태: 달성/미달/판정 보류/판정 대기/미실행), 가설별 표(H1~H4·H7은 M5 결과 참조, H5·H6 이번 판정과 원인 분석 칸), 채점 모델 검증(κ·일치율·표본 구성·사람 점수 분포·`uninterpretable` 여부), H5(13문항 판정 문구·유형별·sql 제외 9문항·실버 29문항 방향 확인·사람 점수 기준 값·3회 편차), Sol 사후 확인, 라우터 dev 판정표와 착수 여부, 충실성 보조표(**'사람 검증 없음'**), 보안(경로·시스템별 n·누설 건수·c1 후보/확정·기준선·에코 제외·c3 제외·대조군 오거절률·부분 답변 비율·노출률·Agentic 시도율·parity·not_found 일치), 지연(구간 통계·P95 판정·CI·"스트리밍 모드에서 측정"·동시성 1·예열 제외), 실패 분석(시스템 × 분류 × 세트, d1·d2·`indeterminate`·도구 부재·거절 위반), 운영 지표, 비용(Gemini 채점비는 평가 비용으로 분리), 측정 환경(`m7_latency.json`의 자동 수집값 + M6 환경 표 링크), 해시·고정 조건 표.
- **사용자 보존 영역**(생성기가 건드리지 않는다): 측정 환경 보충, Gemini 콘솔 Usage 실행 전후와 원장 대조(FP-037), `make test-integration` 실행일·결과, 사용자 메모. 칸만 만들고 값은 '미기록'으로 둔다.

### 2. 운영 지표 집계 (EVAL '운영')
- 질의당 **입력·출력·캐시 토큰, 비용, 단계 수**를 시스템 × 유형별(평균·중앙값·P95)로 집계한다. 범위: M6 test 골드·실버(gpt-6-luna), 보안 실행(`data/interim/m7/security/runs.jsonl`, 있으면), dev 모델 비교는 M6 결과 참조. 비용은 원장 기준(정가 + 부가세)이며 Gemini 채점·probe·파일럿 비용은 **평가 비용**으로 `run_id`별로 분리해 보고한다(시스템 운영비와 섞지 않는다).
- 재집계 값이 M6 보고서와 같아야 한다: M6 test 골드의 단순 RAG 비용 합 $0.0140144·Agentic $0.0329375, 실버 단순 $0.0414915·Agentic $0.11389(회귀 테스트는 fixture, 실제 데이터 대조는 `make m7-report`가 불일치 시 실패 메시지를 출력).

### 2-1. 사용자 실행 절차 절(보존 영역, 생성기가 틀로 만든다)
M6 결과 문서의 '사용자 실행 절차'와 같은 형식으로 아래 순서를 번호 목록으로 적는다. 9번 항목에는 반드시 다음 문구를 넣는다: "step 7~9 실행 후 `make up`과 함께 **`make m6-serve`를 별도 터미널에서 실행**한 뒤 `make m7-security-set` → `make m7-security-dry` → `make m7-security`(MCP 재생은 실행 중인 서버에 연결)".
1. `uv add google-genai`(사용자 직접) 2. step 0~6 실행·리뷰 3. `make m7-probe` 4. `make m7-pilot` 5. 루브릭 확정·커밋 6. `make m7-cards` → 사람 채점(정답성만) → CSV 저장 → `make m7-human-register` → 커밋 7. `make m7-freeze` 8. `make m7-grade-dry` → `make m7-grade`(2026-12-31 전 완료) 9. 위 문구의 보안 실행, 이어서 `make m7-leak-review-register`(c1 후보 확인 CSV 저장 후) 10. `make m7-analyze` 11. step 10 실행 후 `make m7-latency-dry` → `make m7-latency`(동시성 1 단독, 다른 부하 없이) 12. `make m7-failures`, `make m7-report` 13. Gemini 콘솔 Usage와 원장 대조(FP-037), `make test-integration` 결과 기록 14. 결과 산출물·문서 커밋 전 `make verify`.

### 3. 한계 절(보존 영역, 생성기가 틀로 만들고 이후 사용자가 보충)
아래 항목이 **모두 문장으로 들어간다**(테스트가 문자열을 검사한다):
- 블라인드 한계: SQL 결과 인용이나 답변 형식으로 채점자(Gemini·사람)가 시스템을 추정할 수 있다.
- 충실성 지표는 사람 검증이 없다(정답성만 사람 검증).
- 답변이 시스템·문항당 1회라 비결정성이 점수에 섞인다.
- 사람 채점자가 1인이라 채점자 간 일치를 잴 수 없다.
- "스트리밍 모드에서 측정"한 지연이며 외부 API 지연 변동이 있다.
- 유료 등급 Gemini는 입력을 제품 개선에 쓰지 않는다(출처 https://ai.google.dev/gemini-api/docs/pricing, 확인일 2026-10-01).
- 합성 문항이 실제보다 쉽다는 점(EVAL 규칙), H6 0건은 관측값이며 95% 상한 3/n이라는 점, MCP 경로는 도구 수준이라는 점, c1 판정은 사용자 확인에 의존한다는 점, M6 한계(문장형 정답 값으로 정답 값 포함률이 무력)와 Luna 부분 답변 경향의 결과 연결.

### 4. Makefile·AGENTS.md
`m7-report`(`uv run python -m kodocs.eval.m7_report`, 네트워크 없음, 생성 영역만 갱신)를 `.PHONY`·Makefile·AGENTS.md 명령어 목록에 추가한다. AGENTS.md의 명령어 목록에서 `make eval`·M7 항목 설명을 실제 타깃과 일치하도록 정리한다(이미 각 step이 추가한 항목 외에 중복·누락만 점검).

### 5. 테스트
- **fixture로 모든 칸을 채웠을 때 `미실행`이 데이터가 있는 칸에 나오지 않는다**(step 0의 표시 버그 회귀 방지: 칸의 값 출처를 manifest 키가 아니라 데이터 존재 여부로 판단). 일부 manifest만 있을 때는 없는 항목만 `미실행`·`판정 대기`다.
- H6가 c1 후보 ≥ 1 & 확인 미등록이면 보고서에 `판정 대기(c1 후보 확인 필요)`가 나온다. κ 미달 fixture에서 H5 문구에 `채점 모델 검증 미달, 사람 채점 기반`이 나온다.
- 운영 지표 재집계가 M6 보고서 값과 같다(위 비용 값). Gemini 비용이 시스템 운영비에 섞이지 않는다.
- 한계 절 필수 문장(위 목록) 검사. 충실성 표에 '사람 검증 없음'. 스트리밍·유료 등급 문구. 생성이 보존 영역을 바꾸지 않는다(생성 영역 밖 바이트 동일).

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_report'
make m7-report   # 네트워크 없음. 데이터가 없는 칸은 '미실행'으로 나와야 한다
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 생성기가 데이터 존재 여부로 `미실행`을 판단하는가(특정 manifest 키 부재가 아니라)?
   - 한계 절에 블라인드·충실성 검증 없음·1회 실행 비결정성·채점자 1인·스트리밍·유료 입력 비학습 문장이 모두 있는가?
   - H5·H6 문구가 hypotheses.md M7 원문과 같고 보고서 어디에서도 규칙·수치를 새로 정하지 않는가?
   - 생성 영역 밖(보존 영역)이 재생성 후에도 바이트 동일한가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- H5·H6·κ·P95 판정 문구나 기준을 이 문서에서 새로 쓰거나 바꾸지 마라. 이유: 결과를 보기 전에 사전 등록한 원문을 그대로 사용해야 한다(M7-H1·L4·K3·P3).
- M5·M6 결과 수치를 재계산하거나 문서 내용을 바꾸지 마라(링크·인용만). 이유: 소급 변경 금지(M6 규칙 16, ADR-026).
- 보존 영역(콘솔 Usage·통합 테스트 결과·사용자 메모)에 값을 지어내서 채우지 마라. 이유: 확인되지 않은 값은 '미기록'이다(FP-044·FP-037).
- Gemini 채점·probe·파일럿 비용을 시스템 운영 비용(질의당 비용)에 합산하지 마라. 이유: 평가 비용과 운영 비용은 다른 질문의 답이다.
- `make m7-report`를 데이터가 없는 상태에서 성공처럼 보이게 만들지 마라(빈 칸을 0이나 통과로 채우지 말 것). 이유: 형식만 보고 성공 판정하는 조용한 실패다(FP-001).
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
