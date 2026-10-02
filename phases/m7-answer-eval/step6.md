# Step 6: analysis-stats

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-K3·K4·H1·H2·SOL1·R1·G3 (κ·H5 문구·Sol 사후·라우터·3회 편차)
- `src/kodocs/eval/m5_stats.py`(부트스트랩·시드 규칙)와 M6에서 이를 재사용한 코드(`m6_dev.py` 등) — **재사용**하고 새로 구현하지 않는다
- `src/kodocs/eval/m7_grading.py`·`m7_human.py`·`m7_grade.py`·`m7_config.py` — 산출물 경로·`load_human_grades`·`m7_grade_scores.json` 형식
- `data/manifest/m6_eval_inputs.json`(`h5_pool`·`h5_pool_ex_sql`·`h5_silver_direction`·`router_types`)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

채점 결과(점수 manifest)와 사람 채점으로 κ·H5·Sol 사후·라우터 판정표를 계산한다. 모듈은 `src/kodocs/eval/m7_stats.py`(순수 함수)와 `src/kodocs/eval/m7_analyze.py`(CLI)다. 이 step은 **H6을 다루지 않는다**(보안 결과는 step 9에서 같은 CLI에 추가).

### 1. 순수 함수 (`m7_stats.py`)
- `question_scores(scores, classes) -> dict[(question_id, system), float]`: 시스템별 정답성 3회 평균. `det_zero`는 0(3회 모두 0으로 취급), `not_scored`는 제외. `median_int(scores3) -> int`(κ용).
- `weighted_kappa(a, b, *, weights: Literal["quadratic","linear"], categories=(0,1,2,3)) -> float`와 `kappa_report(human, gemini_median) -> KappaReport`: 이차 가중 κ(주), 선형 가중 κ, 정확 일치율, ±1 이내 일치율, 3회 개별 κ, 부트스트랩 95% CI(10,000회, 시드 20261001), 사람 점수의 최빈값 비율(80% 이상이면 `uninterpretable=True`), `passed = quadratic_kappa >= 0.6`. 외부 통계 패키지를 추가하지 않는다(순수 구현, 손계산 fixture로 검증).
- `h5_report(...) -> H5Report`: 13문항 짝 비교 부트스트랩(10,000회, **시드 20260930**, `m5_stats` 함수 재사용) 차이(Agentic − 단순)·95% CI·`verdict_text`(M7-H1의 세 문구 **상수** — 채택/기각(반대 방향)/판정 보류. CI 경계가 정확히 0이면 보류). 병기: 유형별 기술 통계(identifier 4·sql 4·multi_doc 5), sql 제외 9문항(같은 방법, 판정 아님, 13 채택·9 CI가 0 포함이면 `depends_on_sql=True`), 실버 test 29문항(identifier 15·multi_doc 14) 방향 확인(판정 아님), 3회 표준편차, **사람 점수 기준 값**(사람이 채점한 H5 답변 + 결정론 0). `kappa_passed=False`이면 판정 입력을 사람 점수로 바꾸고 문구에 `채점 모델 검증 미달, 사람 채점 기반`을 붙인다(M7-K4). 사람 점수가 H5 답변을 다 덮지 못하면 `HumanCoverageError`.
- `sol_report(...)`: dev 정답성(3회 평균)을 모델(luna·sol) × 시스템 × 유형으로 집계하고 같은 문항 짝 비교(Sol − Luna) 95% CI. CI가 0을 배제하며 Sol 우세면 `recheck_signal=True`("선택 재검토 신호"). 판정이 아님을 표에 표시한다.
- `router_report(...)`: luna dev 두 시스템(골드 20 + 실버 55)의 `expected_response=answer` 문항에서 8문항 이상인 유형별 (Agentic − 단순) 평균 차이. 착수 = (+0.5 이상 유형 ≥ 1) and (−0.5 이하 유형 ≥ 1), **경계 포함**(정확히 ±0.5도 해당). 유형별 문항 수·평균 차이·`launch`·유형별 승자 라우팅 표(조건 만족 유형은 dev 승자, 나머지는 Agentic)를 낸다. 착수하면 `m7b-router` phase를 별도 설계해야 한다는 문구를 출력한다.
- `faithfulness_table(...)`: 충실성 3회 평균 표(시스템·유형별). **표에 "사람 검증 없음(보조 지표)"가 자동으로 붙는다.**
- 3회 편차 집계: 문항별 표준편차 평균·3회 전부 일치 비율(정답성·충실성 각각).

### 2. CLI (`make m7-analyze`)
- **분석 전 원본 해시 대조(M7-D1 예방, FP-053)**: `m7_rawhash.verify_fingerprint`로 현재 `data/interim/m7/grades.jsonl`이 `m7_grade_summary.json`의 가장 최근 `raw` 기록과 같은지, `m7_grade_scores.json`에 기록된 fingerprint와 같은지 확인하고 다르면 `RawHashMismatch`로 **분석을 거부**한다. 또 `verify_m6_inputs()`(step 3)를 다시 실행해 M6 입력이 freeze 기록(`m7_config.json`의 `runs.jsonl` fingerprint)과 같을 때만 진행한다.
- 입력: `data/manifest/m7_grade_scores.json`, `data/manifest/m7_human_grades.json`(+ `load_human_grades`), `m6_eval_inputs.json`, `m7_config.json`. 출력: `data/manifest/m7_analysis.json`(점수·통계·판정 문구, **텍스트 없음**)와 표준 출력 요약.
- **사람 CSV 등록 manifest가 없으면 κ 계산을 거부한다**(`HumanGradesNotRegistered`). 채점 manifest가 불완전(우선순위 tier 1·2가 덜 끝남)이면 어떤 문구도 만들지 않고 부족한 건수를 알린다.
- `m7_analysis.json`에는 **H6 섹션이 없다**. step 9가 `h6` 키를 추가한다(키가 없을 때 출력은 `H6: 보안 결과 없음(step 9 이후)`).
- Makefile 타깃 `m7-analyze`, `.PHONY`, AGENTS.md 명령어 항목(`make m7-analyze  # κ·H5·Sol 사후·라우터 dev 판정 → data/manifest/m7_analysis.json — 네트워크 없음, 사용자 실행`).

### 3. 테스트
- κ: 완전 일치 = 1, 손계산한 3×3·4×4 예제(이차·선형 값을 fixture에 적어 둔다), 한 범주만 쓴 경우(분산 0) 처리, 사람 점수 80% 몰림 시 `uninterpretable`.
- 판정 문구 3종 분기와 경계(CI 하한이 정확히 0 → 보류, 상한이 정확히 0 → 보류). 부트스트랩이 `m5_stats` 함수와 시드 20260930을 쓴다(같은 입력 → 같은 CI). κ 미달 → 사람 점수 기반 문구. `depends_on_sql`.
- 라우터: +0.5·−0.5 정확히 경계(포함), 7문항 유형 제외, 한쪽만 있을 때 `launch=False`.
- **원본 해시 대조**: `grades.jsonl`이 기록과 다르면(줄 수정·추가·삭제) 분석을 거부한다. M6 입력이 freeze 기록과 다르면 거부한다.
- 충실성 표에 '사람 검증 없음' 표기. 사람 CSV 미등록 시 κ 거부. 채점 불완전 시 문구 미생성.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_stats or m7_analyze'
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - κ가 이차 가중 주·선형 보조이고 사람 점수와 Gemini **중앙값**을 비교하는가?
   - H5 부트스트랩이 `m5_stats` 재사용·시드 20260930·10,000회인가? κ 부트스트랩은 시드 20261001인가?
   - 판정 문구가 hypotheses.md M7-H1의 원문과 같은가?
   - 충실성 표에 '사람 검증 없음'이 붙는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- H5 판정 문구·라우터 착수 조건·κ 기준(0.6)을 이 step에서 바꾸지 마라. 이유: 결과를 보기 전에 사전 등록한 값이다(M7-H1·R1·K3).
- scipy·sklearn 등 통계 패키지를 추가하지 마라. 이유: 의존성 추가는 승인 대상이며 κ는 순수 구현으로 손계산 검증이 가능하다.
- `make m7-analyze`를 실제 데이터로 실행해 결과를 보고하지 마라. 이유: 채점·사람 채점 전 실행은 의미가 없고 결과를 본 뒤 규칙을 건드릴 위험이 있다. 테스트는 fixture만 쓴다.
- H6(누설) 문구·집계를 이 step에서 만들지 마라. 이유: 보안 결과와 c1 확인 CSV 의존이 있어 step 9에서 한 곳에서 처리한다.
- 사람 점수가 없을 때 Gemini 점수로 κ를 대신 계산하지 마라. 이유: 검증 대상을 자기 자신으로 검증하게 된다.
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
