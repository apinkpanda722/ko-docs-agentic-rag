# Step 4: human-cards

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-K1~K4·M7-X1·M7-G6
- `src/kodocs/eval/m7_grading.py`·`src/kodocs/eval/templates/m7_rubric.md` — step 3 산출물(`load_m6_answers`·`classify_all`·`build_grade_item` 근거 텍스트 로더·`parse_rubric_sections`·`select_pilot`)
- `data/manifest/m6_eval_inputs.json` — `h5_pool.ids`(13문항)
- `src/kodocs/eval/m5_stats.py` — 시드·부트스트랩 함수 스타일(참고)
- `.gitignore` — `data/interim/`가 무시되는지 확인

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

사람 채점용 κ 표본 50개를 뽑고, 로컬 오프라인 카드와 CSV 등록 도구를 만든다. 모듈은 `src/kodocs/eval/m7_human.py`다. 사람은 **정답성만** 채점한다.

### 1. 표본 추출 (M7-K1)
- `select_kappa_sample(answers, classes, h5_ids, pilot_ids, *, seed=20261001, n=50) -> KappaSample`.
  - 풀: gpt-6-luna의 `gemini_acc` 답변(dev·test)에서 **파일럿 10건(`data/manifest/m7_pilot.json`의 ids, 없으면 `select_pilot`의 결정론 결과)을 제외**한다.
  - (가) 강제 포함: `h5_pool.ids` 13문항의 두 시스템 답변 중 풀에 있는 것(결정론 0인 단순 RAG sql 답변은 `gemini_acc`가 아니라 자동 제외; 최대 22개).
  - (나) 나머지 `n - (가)`개: 시스템 × 출처(real·synthetic) 4층에서 층당 균등 배분(나머지는 큰 층부터 하나씩), 층 내 `random.Random(seed)`로 단순 무작위(정렬된 answer_id 기준). 층이 모자라면 남는 몫을 다른 층에 배분한다. 결정론이어야 한다.
  - 결과 `KappaSample`: `answer_ids`(정렬), `forced_h5`(수), `strata`(층별 건수), `sample_sha256`.
- `data/manifest/m7_human_sample.json`(커밋)에 `answer_ids`(정렬 순서, 카드 순서 아님)·강제 포함 수·층별 건수·`sample_sha256`·시드·파일럿 제외 수만 기록한다. **카드 번호 ↔ answer_id 대응은 이 manifest에 넣지 않는다**(채점자가 시스템을 알 수 없게).

### 2. 오프라인 카드 HTML
- `make m7-cards` → `data/interim/m7/cards/cards.html`(비커밋)과 `key.json`(카드 번호 `c-001`… ↔ answer_id, 비커밋), `cards_manifest.json`(루브릭 해시·표본 해시·key 해시·생성 시각, 비커밋).
- 카드 한 장에 **질문, 참조 답변, 시스템 답변, 인용 근거(구간별), 정답성 0~3 입력(라디오), 메모**를 둔다. 정답성 앵커 문구는 `parse_rubric_sections`로 루브릭 파일에서 **그대로** 가져와 페이지 상단에 표시한다(Gemini와 같은 문구). 카드 순서는 시드 20261001로 섞는다. **시스템·모델·도구·트레이스·Gemini 점수·유형 태그(sql 등)·`gemini_acc` 같은 분류명은 표시하지 않는다.**
- 단일 HTML 파일(인라인 CSS·JS), **외부 URL·외부 스크립트·폰트 요청 없음**. 진행 상황은 `localStorage`에 임시 저장하되 모든 접근을 try/catch로 감싼다(저장소가 막혀도 동작). 하단 버튼으로 CSV(`card_id,correctness` 두 열만)를 내려받게 한다. 미입력 카드가 있으면 내보내기 전에 표시한다.
- 카드 HTML에는 실제 규정 원문이 들어간다. 파일은 `data/interim/m7/cards/`에만 두며 커밋·게시·외부 전송하지 않는다(경로가 gitignore 대상임을 테스트로 확인한다: `git check-ignore -q data/interim/m7/cards/cards.html`).

### 3. 사람 채점 CSV 등록
- 사람 CSV 위치: `data/golden/m7_human/m7-human-grades.csv`(**사람만 작성**, 에이전트는 만들지 않는다). 열은 `card_id,correctness` **두 개뿐**이다.
- `make m7-human-register` → `register_human_grades(csv_path, cards_dir)`: 검증 후 `data/manifest/m7_human_grades.json`(커밋)에 CSV SHA-256·행 수·점수 분포·`cards_manifest`의 루브릭 해시·표본 해시·key 해시·등록 시각을 기록한다(덮어쓰기는 거부한다. 재등록은 `--force-reason "<사유>"`가 있어야 하며 이전 해시를 이력에 남긴다).
- 검증: 정확히 50행, 모든 `card_id`가 `key.json`에 있고 중복·누락 없음, `correctness`는 정수 0~3, **열이 `card_id`·`correctness` 외에 있으면 거부**(메모·점수 외 정보 금지), 표본 해시가 `m7_human_sample.json`과 같음.
- `load_human_grades(manifest_path, csv_path, key_path) -> dict[answer_id, int]`를 제공한다(step 6이 κ 계산에 쓴다). 등록 manifest가 없으면 `HumanGradesNotRegistered`를 올린다.

### 4. 블라인드 강제 (테스트)
- `m7_human.py`는 `grades.jsonl`·`m7_grade`·`gemini_chat`·Gemini 점수 manifest(`m7_grade_scores.json`)를 import하거나 읽지 않는다(AST·문자열 테스트). 카드 HTML에 `simple`·`agentic`·`gpt-6`·`run_sql`·`lookup_id`·`gemini` 문자열이 없다.

### 5. Makefile·AGENTS.md
- 타깃 `m7-cards`, `m7-human-register`를 `.PHONY`와 Makefile에 추가하고 AGENTS.md 명령어 목록에 항목을 적는다(`make m7-cards  # κ 표본 50개 + 오프라인 카드 HTML 생성(로컬 전용, 네트워크 없음) — 사용자 실행`, `make m7-human-register  # 사람 채점 CSV 검증·해시를 data/manifest/m7_human_grades.json에 기록 — 사용자 실행`).

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_human'
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 표본이 정확히 50개이고 파일럿 10건이 포함되지 않았는가? 같은 시드로 두 번 뽑으면 같은 `sample_sha256`인가?
   - 카드 HTML에 외부 URL이 없고 시스템·모델·Gemini 점수가 없는가?
   - `m7_human.py`가 Gemini 채점 모듈·점수 파일을 import하지 않는가?
   - CSV 등록이 `card_id,correctness` 이외의 열을 거부하는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `data/golden/m7_human/`에 CSV를 만들거나 점수를 채우지 마라(테스트는 tmp 경로 fixture만). 이유: 사람 채점은 사람만 작성하며 에이전트가 만들면 κ 검증이 무의미하다.
- 카드 번호 ↔ answer_id 대응을 커밋되는 파일에 넣지 마라. 이유: 채점자가 시스템·유형을 역추적할 수 있다(블라인드).
- 카드 HTML·`key.json`을 `data/interim/m7/cards/` 밖이나 커밋 대상에 두지 마라. 이유: 실제 규정 원문이 들어 있다(ADR-002 로컬 전용).
- 카드에 Gemini 점수·참조 점수·`gemini_acc` 분류 같은 힌트를 넣지 마라. 이유: 사람이 Gemini보다 먼저 독립적으로 채점했다는 것이 이 검증의 전제다(M7-X1).
- 충실성을 사람 채점 항목에 넣지 마라. 이유: 사람 검증은 정답성만 하며 충실성은 사람 검증이 없는 보조 지표다(M7-K2).
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
