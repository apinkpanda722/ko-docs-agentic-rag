# Step 3: grading-inputs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-D1·M7-G1·M7-G2·M7-G3·M7-G6 (입력 데이터·결정론 분류·블라인드 입력·파일럿 규칙)
- `src/kodocs/llm/gemini_chat.py`(`GradeRequest`·`GradeResult`) — step 2 산출물
- `src/kodocs/eval/m6_runner.py`·`m6_dev.py`·`m6_test.py`·`m6_config.py`·`m6_metrics.py`·`m6_inputs.py` — M6 실행 기록(runs.jsonl) 읽기·해시 검증 방식, 질문 로딩(`load_m6_questions`)
- `src/kodocs/eval/golden.py`(`Question`·`Asker`·`Qrel`)와 `data/golden/queries/gold.jsonl`·`silver.jsonl` 필드(`expected_response`·`answer_values`·`qrels`·`asker`·`type`·`source`·`split`)
- `data/interim/m6/dev/runs.jsonl`, `data/interim/m6/test/run-01/runs.jsonl`, `data/interim/m6/test/run-01/silver/*.jsonl`의 **실제 한 줄**(비커밋 로컬 파일, 필드 이름 확인용: `question_id`·`system`·`model`·`answer`·`citations`·`refused`·`refuse_reason`·`status`·`tool_refs` 등)
- `data/manifest/m6_config.json`(`m6_dev_sha256`), `data/manifest/m6_test_runs.jsonl`, `data/manifest/m6_test_results/`
- `src/kodocs/chunking/`·`src/kodocs/retrieval/index_build.py` — 인덱스 빌드가 문서·조문·합성 문서 본문을 오프라인으로 불러오는 방식(근거 텍스트 로더가 재사용할 함수를 찾는다)
- `src/kodocs/eval/templates/` — 기존 템플릿 파일 위치·형식

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

Gemini 채점의 **입력 쪽**을 만든다(호출은 step 5). 모듈은 `src/kodocs/eval/m7_grading.py`, 루브릭은 `src/kodocs/eval/templates/m7_rubric.md`다.

### 1. M6 실행 기록 로더 + 해시 검증 (M7-D1)
- `load_m6_answers(root=Path(".")) -> list[AnsweredRun]`: dev(`runs.jsonl`, `run-01-invalid` 제외)·test 골드(`run-01/runs.jsonl`)·test 실버(`run-01/silver/`)를 읽어 `answer_id = f"{run_set}|{question_id}|{system}|{model}"`(run_set ∈ `dev`·`test_gold`·`test_silver`)를 붙인다. `AnsweredRun`에는 질문 필드(유형·출처·`expected_response`·참조 답변 경로·asker)와 실행 기록(answer·citations·refused·refuse_reason·status·system·model·tool_refs)과 `answer_sha256`(답변 텍스트 UTF-8 SHA-256)을 담는다. answer_id 중복이 있으면 오류다.
- 해시 검증: dev `runs.jsonl`의 SHA-256 = `m6_config.json`의 `m6_dev_sha256`. test 결과는 M6 `m6_test.py`·`m6_report.py`가 결과 해시를 계산·기록한 방식을 읽고 **같은 방식**으로 검증한다. 불일치하면 `InputHashError`로 멈춘다. 실제 `runs.jsonl` 필드 이름은 파일 한 줄을 직접 읽어 확인한다(FP-011, 필드명 추측 금지).
- 중복 행(재시도·append-only)이 있으면 M6 코드가 쓰는 최신 행 선택 규칙(`latest_records` 계열, FP-032)을 따른다.

### 2. 결정론 분류 (M7-G1)
- `classify_run(run, question) -> Literal["det_zero", "gemini_acc", "gemini_faith_only", "not_scored"]`를 M7-G1 그대로 구현한다. 문항 필드 `expected_response`는 `answer`·`no_answer`·`indistinguishable_from_no_answer` 세 값뿐이다. `det_zero`의 점수는 정답성 0이며 충실성은 '해당 없음'이다.
- `classify_all(runs) -> dict[answer_id, class]`와 집계 `class_counts(...)`(세트·모델·시스템별 건수)를 둔다. (참고: 현재 M6 산출물의 `gemini_acc`+`gemini_faith_only`는 dev 171·test 골드 35·test 실버 93 = 299개로 예상된다. 이 값은 테스트로 강제하지 말고 `make m7-grade-dry`(step 5)가 출력해 사용자가 확인한다.)

### 3. 블라인드 입력 빌더 + 근거 텍스트 (M7-G2)
- `build_grade_item(run, question, evidence_loader) -> GradeItem(answer_id, mode, request_text, evidence_chars)`. `mode`는 `acc`(정답성+충실성) 또는 `faith`(충실성만).
- 사용자 메시지는 질문·참조 답변·시스템 답변·인용 근거 네 섹션만 담는다. **시스템 이름·모델명·도구 이름(`run_sql`·`lookup_id` 등)·트레이스·단계 수·점수 필드를 넣지 않는다.** 참조 답변과 시스템 답변은 **원문 그대로**(정규화·정제·공백 변경 없이) 넣는다(FP-020).
- 참조 답변은 로컬에서 읽는다: 실제 규정 문항은 `data/interim/golden/`의 비커밋 참조 답변(ADR-025), 합성 문항의 참조 답변 위치는 `golden.py`·`m4`·`golden_review` 코드로 확인한다. 참조 답변 SHA-256이 질문 레코드의 `reference_sha256`과 같은지 검증하고 다르면 오류다.
- 인용 근거: 답변이 인용한 유효 인용(`citations`: `doc_id`·`section_key`)의 로컬 원문. 실제 규정은 해당 조(`section_key`)의 본문, 합성 문서(`section_key`가 None)는 문서 본문 앞부분. **오프라인 문서 로더**는 `index_build.py`·`chunking/`이 쓰는 로더를 재사용한다(파싱을 새로 구현하지 않는다). 재사용할 함수가 없으면 최소한의 어댑터를 만들고 최종 보고에 이유를 적는다. 근거 총합은 6,000자 이내이며 인용 순서대로 라운드로빈(각 인용에서 앞부분부터 번갈아 채움)으로 자른다. 근거를 못 찾은 인용은 건너뛰고 건수를 `evidence_missing`으로 기록한다.
- `shuffle_order(answer_ids, seed=20261001) -> list[str]`: 입력 정렬 후 시드 셔플(결정론). 호출 순서 전용이다.

### 4. 루브릭·스키마 (`templates/m7_rubric.md`)
- 마크다운 파일 구조(파서가 읽는다): `## 공통 지침`, `## 정답성 앵커`, `## 충실성 앵커`, `## 출력 형식`. 한국어로 쓰고 초안임을 헤더 주석으로 적는다(사용자가 파일럿 후 확정·커밋한다).
- 정답성 앵커(0~3): 3 = 참조 답변의 모든 핵심 사실(값·ID·조항 번호)이 일치하고 모순 없음 / 2 = 핵심은 맞으나 사소한 누락·부정확이 있음 / 1 = 일부만 맞거나 핵심 값 중 일부가 틀림 / 0 = 틀리거나 무관하거나 근거 없다고 답함. 충실성 앵커(0~3): 3 = 모든 주장이 인용 근거로 뒷받침됨 / 2 = 대부분 뒷받침되고 사소한 추정이 섞임 / 1 = 상당 부분이 근거에 없음 / 0 = 근거와 모순되거나 대부분 근거에 없음. 공통 지침: 문체·길이는 보지 않는다, 숫자·ID·날짜의 정확성을 우선한다, 시스템 정체를 추측하지 않는다.
- 출력 스키마(pydantic): `AccGrade(correctness: int 0..3, faithfulness: int 0..3, rationale: str ≤200자)`, `FaithGrade(faithfulness: int 0..3, rationale: str ≤200자)`. 범위·길이 위반은 검증 오류(= 형식 오류)다.
- `rubric_sha256()`(루브릭 파일 텍스트 SHA-256), `schema_sha256()`(두 스키마의 JSON 스키마 직렬화 SHA-256), `parse_rubric_sections(path) -> dict[str, str]`(step 4의 카드가 '정답성 앵커' 문구를 그대로 가져가는 데 쓴다).

### 5. 파일럿 표본 선정 (M7-G6)
- `select_pilot(runs, classes, *, seed=20261001, n=10) -> list[answer_id]`: gpt-6-luna dev 답변 중 `gemini_acc`인 것을 `answer_id` 정렬 후 `random.Random(seed).sample`로 추출한다. 결정론이어야 한다. (실제 기록·manifest 쓰기는 step 5의 `make m7-pilot`이 한다.) 순수 함수와 테스트만 만든다.

### 6. 테스트 (실제 M6 비커밋 파일에 의존하지 않는다 — tmp fixture)
- 분류 4종과 경계(refused+answer → det_zero, status 실패 → det_zero, no_answer+refused=false → gemini_faith_only, no_answer+refused=true → not_scored, 빈 답변)·answer_id 유일성·해시 불일치 시 `InputHashError`.
- 블라인드: 프롬프트·요청 본문에 시스템 이름(`simple`·`agentic`)·모델 이름(`gpt-6-luna` 등)·도구 이름·`tool_refs`·`steps`·점수 문자열이 없다. 참조·답변 텍스트가 입력과 **바이트 동일**(특수문자 `?&oc=`·NFC/NFD·연속 공백 포함, FP-020).
- 근거 6,000자 상한·라운드로빈·누락 인용 기록. 같은 시드 → 같은 셔플·같은 파일럿 10건. 루브릭 섹션 파싱과 해시 안정성.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_grading'
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 실제 `runs.jsonl` 한 줄을 읽어 필드 이름을 확인하고 fixture가 같은 키를 쓰는가?
   - 빌더가 참조 답변·답변 텍스트를 바이트 그대로 전달하는가(정제·정규화 없음)?
   - AGENTS.md나 Makefile을 바꾸지 않았는가(이 step은 CLI 타깃이 없다)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 답변·참조 답변에 정규화·공백 정리·마스킹을 적용하지 마라. 이유: 채점 입력이 바뀌면 점수가 원문과 무관해진다(FP-020).
- 프롬프트에 시스템명·모델명·도구 호출·트레이스를 넣지 마라. 이유: 블라인드 채점이 무너진다(M6 규칙 3).
- 문항 점수를 정답 값 포함률 같은 결정론 지표로 대체하지 마라. 이유: M6에서 문장형 정답 값 43/56이 어미 차이만으로 0점이 되어 무력했다(FP-051). 이 step은 4종 분류 외의 결정론 채점을 만들지 않는다.
- M6 `runs.jsonl`·manifest를 수정하거나 답변을 다시 생성하지 마라. 이유: M6 산출물을 재사용한다(M7-D1).
- 루브릭을 확정본처럼 단정하지 마라. 이유: 파일럿 후 사용자가 확정·커밋한다(M7-G4). 헤더에 초안임을 적는다.
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
