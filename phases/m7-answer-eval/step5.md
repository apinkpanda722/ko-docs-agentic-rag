# Step 5: grade-runner

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/results/hypotheses.md`의 M7-X1·X2·G1~G6·K1·D1 (실행 순서·freeze 거부 조건·채점 중단 조건·파일럿)
- `src/kodocs/llm/gemini_chat.py`(`GeminiGrader`·`GradeStopError` 계열·`PriceExpiredError`)·`src/kodocs/costs.py`(`BudgetGuard`·원장) — step 2 산출물
- `src/kodocs/eval/m7_grading.py`·`m7_human.py`·`m7_rules.py`·`templates/m7_rubric.md` — step 1·3·4 산출물
- `src/kodocs/eval/m6_config.py`(freeze·덮어쓰기 거부 방식)·`m6_dev.py`·`m6_test.py`(dry-run 기록 `m6_dry_runs.jsonl`·재개·잠금 방식)
- `data/manifest/m6_config.json`, `data/manifest/m6_dev.json`
- `Makefile`의 m6 타깃(변수·플래그 전달 방식), `tests/eval/`의 m6 러너 테스트(fake 클라이언트 주입 방식)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

파일럿·freeze·재개 가능한 Gemini 채점기를 만든다. 모듈은 `src/kodocs/eval/m7_config.py`(고정)와 `src/kodocs/eval/m7_grade.py`(실행)다. 모든 Gemini 호출은 `GeminiGrader`만 쓴다.

### 1. 산출물 경로
| 파일 | 커밋 | 내용 |
|---|---|---|
| `data/interim/m7/grades.jsonl` | 아니오 | 호출 단위 append-only 기록: `answer_id`·`answer_sha256`·`mode`·`repeat`(1~3)·`correctness`·`faithfulness`·`rationale`·usage·cost·model_settings_hash·rubric_sha256·시각·attempts |
| `data/interim/m7/pilot/grades.jsonl` | 아니오 | 파일럿 호출 기록(같은 형식) |
| `data/manifest/m7_pilot.json` | 예 | 파일럿 10건 id(호출 전에 기록)·시드·루브릭 초안 해시·형식 오류율·점수 분포·쏠림 보고·비용 |
| `data/manifest/m7_config.json` | 예 | freeze 기록(아래 2) |
| `data/manifest/m7_grade_scores.json` | 예 | `answer_id → {mode, class, correctness:[…3개], faithfulness:[…3개]}`(**점수와 id만, 텍스트 없음**), 완료 시 생성 |
| `data/manifest/m7_grade_summary.json` | 예 | 건수·분류별 건수·형식 오류율·점수 분포(쏠림 포함)·3회 일치율·비용·429 정지 이력 |
| `data/manifest/m7_dry_runs.jsonl` | 예 | dry-run 기록(시각·호출 수·예상 토큰·예상 비용·남은 크레딧 비교) |

`grades.jsonl`은 append-only이므로 읽을 때 같은 키의 중복 줄은 최신 것을 쓴다(FP-032). 캐시 키는 **출력을 결정하는 키**와 같게 한다: `(answer_sha256, rubric_sha256, model_settings_hash, mode, repeat)`(FP-033).

### 2. 파일럿 (`make m7-pilot`)
- 호출 **전에** `m7_pilot.json`에 선정된 10건 id를 기록한다(`select_pilot`, 시드 20261001). 이미 파일이 있으면 같은 선정을 재사용하고 다른 선정이면 거부한다.
- 10건 × 3회 = 30호출을 현재 **초안 루브릭**으로 채점한다(`pilot/grades.jsonl`). 보고: 형식 오류율, 점수 분포·쏠림(루브릭 점검 항목, 중단 조건 아님), 3회 일치율, 비용. `--dry-run` 지원. freeze 전에 실행하는 유일한 호출이다. 파일럿 결과는 분석에 쓰이지 않는다(M7-G6).

### 3. freeze (`make m7-freeze`)
`m7_config.json`을 **새로 만들며 덮어쓰기를 거부한다**(M6 `m6_config.py`와 같은 방식, 재고정은 별도 사유 기록 없이는 불가). 기록: 모델 ID·tier·thinking_level·temperature·seed(null 가능)·`rpm_pacing`·`max_output_tokens`·모델 설정 해시, `rubric_sha256`·`schema_sha256`, `rules_sha256`(`m7_rules.rules_sha256()`), 표본 해시(`m7_human_sample.json`)·사람 CSV 해시(`m7_human_grades.json`)·파일럿 manifest 해시·probe manifest 해시, M6 `config_hash`·`m6_dev_sha256`, **현재 dev `runs.jsonl`의 SHA-256·행 수와 test `runs.jsonl`들의 SHA-256·행 수(`verify_m6_inputs`가 돌려준 값, M7-D1(c))**, 가격 `valid_until`, 시드, 고정 시각.
**freeze는 아래 하나라도 어긋나면 거부한다**(M7-X1):
- `data/manifest/m7_human_grades.json`이 없다(사람 CSV 해시 미기록).
- 카드 manifest(`data/interim/m7/cards/cards_manifest.json`)의 루브릭 해시가 현재 `rubric_sha256()`과 다르다.
- `data/manifest/m7_pilot.json`이 없다. `data/manifest/m7_probe.json`이 없다.
- `models.toml`의 temperature·seed·thinking_level이 probe가 '지원'으로 기록한 값과 어긋난다(미지원인데 값이 있음, probe에 없는 thinking 수준 등). 모델 ID가 비었거나 `ensure_price_valid`가 실패한다.
- `verify_m6_inputs()`(step 3, M7-D1 정정판: `m6_dev.json` 해시 + `analyze_dev` 재실행 비교 + test 재집계 비교)가 실패한다. 현재 dev `runs.jsonl`의 SHA-256이 M7-D1에 적힌 값(`0f25104b…`)과 다르면 거부한다(사유를 메시지에 담는다).

### 4. 채점 (`make m7-grade-dry`, `make m7-grade`)
- freeze 기록이 없으면 `m7-grade`는 거부한다. 실행 시작 때 현재 루브릭·스키마·모델 설정·규칙 해시가 freeze 기록과 같은지 검사한다.
- 대상: `load_m6_answers` 전체를 `classify_all`로 분류해 `gemini_acc`·`gemini_faith_only`만 호출한다(`det_zero`는 정답성 0으로 `m7_grade_scores.json`에만 기록, `not_scored`는 기록만). dev(luna·sol)와 test(luna)를 모두 포함한다(파일럿 10건도 다시 채점한다).
- **우선순위(재개 시에도 동일)**: tier 1 = H5 판정 13문항(`h5_pool.ids`)의 두 시스템 답변, tier 2 = κ 표본(`m7_human_sample.json`), tier 3 = test 나머지, tier 4 = dev. tier 안은 `shuffle_order`(시드 20261001) 순서, 한 답변의 3회 반복은 연속 호출한다.
- **재개**: 캐시 키가 이미 `grades.jsonl`에 있으면 호출하지 않는다(중복 호출 0).
- **RPM 페이싱**은 `GeminiGrader`의 `rpm_pacing`을 쓴다. 진행 줄을 N건마다 `flush=True`로 출력한다(진행·남은 건수·누적 비용; API 키·URL 출력 금지, FP-034).
- **정지**: `DailyQuotaExhausted`·`CreditExhausted`·`UnclassifiedRateLimit` 등 `GradeStopError`가 오면 즉시 멈추고, 남은 건수와 다음 초기화 시각(해당 시)을 출력하고 정지 이력을 summary에 남긴다. 다시 실행하면 캐시로 이어진다. `BillingUncertainError`는 그 호출을 `billing_uncertain`으로 기록하고 다음으로 진행한다(재전송하지 않는다). `FatalLLMError`는 전체를 멈춘다.
- **중단 조건(M7-G5)**: 누적 50호출 이후부터 **형식 오류율(파싱·범위 검증 실패 수 / 누적 호출 수) 2% 초과**일 때만 중단한다. 점수 쏠림(한 값 90% 이상)은 중단하지 않고 summary의 보고 항목으로 남긴다.
- **dry-run**: 호출 수(분류별·tier별·모드별), 예상 입력·출력 토큰, 예상 비용(정가 + 부가세, 최악 `max_output_tokens` 기준 상한 병기), 월 상한(`MONTHLY_BUDGET_USD`) 대비, 페이싱에 따른 최소 소요 시간, `ensure_price_valid` 결과를 출력하고 `m7_dry_runs.jsonl`에 기록한다. 선택 플래그 `--credit-usd X`: 예상 비용과 이미 쓴 Gemini 비용(원장 `tier=paid`)의 합을 X와 비교해 출력하고, 실행 중에도 누적(원장 기준, 부가세 포함)이 X를 넘기 전에 정지한다. 분류별 건수(참고: gemini_acc+gemini_faith_only 예상 299개, 호출 897회)를 함께 출력한다. 예상과 다르면 실패시키지 말고 그대로 출력한다.
- 호출 전 월 상한(BudgetGuard)과 `ensure_price_valid`를 검사한다. 완료 시 `m7_grade_scores.json`·`m7_grade_summary.json`을 쓴다.

### 4-1. 실행 산출물 원본 해시 기록 (M7-D1 예방 규칙, FP-053)
- 공용 모듈 `src/kodocs/eval/m7_rawhash.py`: `fingerprint(path) -> {"sha256","rows"}`(UTF-8 바이트 SHA-256과 비어 있지 않은 줄 수), `record_fingerprint(manifest_path, key, path, event)`(manifest의 `raw` 이력 리스트에 `{key, path, sha256, rows, event, at}` append), `verify_fingerprint(manifest_path, key, path)`(현재 원본이 **가장 최근 기록**과 같아야 하며 다르면 `RawHashMismatch`). step 6·9·10·13이 이 모듈을 재사용한다.
- `make m7-pilot`은 완료·정지 시 `pilot/grades.jsonl`의 fingerprint를 `data/manifest/m7_pilot.json`의 `raw`에, `make m7-grade`는 완료·**정지(`GradeStopError` 포함)** 시 `data/interim/m7/grades.jsonl`의 fingerprint를 `data/manifest/m7_grade_summary.json`의 `raw`에 기록한다(append-only 파일이 재개 실행으로 자라므로 실행마다 새 항목을 추가한다). `m7_grade_scores.json`도 생성 시 그 시점의 `grades.jsonl` fingerprint를 담는다.
- `m7-grade`는 **시작할 때** 기존 `grades.jsonl`이 가장 최근 기록과 같은지 `verify_fingerprint`로 확인하고 다르면 거부한다(실행 사이의 수정·손상 탐지). 이후 분석(step 6의 `m7-analyze`)은 현재 `grades.jsonl`이 기록과 같을 때만 진행한다.

### 5. Makefile·AGENTS.md
`m7-pilot`, `m7-freeze`, `m7-grade-dry`, `m7-grade`(+ `--credit-usd` 전달을 위한 변수 `M7_CREDIT_USD`)를 `.PHONY`와 Makefile에 추가하고 AGENTS.md 명령어 목록에 각각 적는다(실제 API를 호출하는 `m7-pilot`·`m7-grade`는 "실제 Gemini 호출·과금, 사용자 실행"으로 표시).

### 6. 테스트 (fake `GeminiGrader` 주입, 네트워크 금지)
- 중단·재개 시 이미 기록된 키는 호출 0회. 일일 한도 예외 → 즉시 정지·정지 이력·재실행 시 이어짐. 크레딧 소진 정지. 형식 오류율: 49호출까지는 2% 초과여도 중단하지 않고, 50호출 이후 2% 초과면 중단, 점수가 한 값에 몰려도 중단하지 않고 summary에 쏠림이 기록됨.
- **원본 해시 기록**: pilot·grade 실행(완료와 정지 각각)이 `raw` 이력에 sha256·행 수를 남기고, 재개 실행이 새 항목을 추가하며, 시작 시 파일이 기록과 다르면 거부한다. `verify_fingerprint`가 한 줄 수정·줄 추가·줄 삭제를 모두 탐지한다. `m7_rawhash`는 텍스트를 manifest에 쓰지 않는다.
- freeze 거부 각각(사람 CSV 해시 없음·루브릭 해시 불일치·파일럿 manifest 없음·probe 불일치)과 덮어쓰기 거부. freeze 전 `m7-grade` 거부. 모델 ID 빈 값 거부. `valid_until` 이후 거부. 우선순위(tier 순서) 테스트. `det_zero`·`not_scored`는 호출 0회. `--credit-usd` 초과 전 정지. `m7_grade_scores.json`에 텍스트가 없다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/eval -k 'm7_config or m7_grade'
make -n m7-pilot m7-freeze m7-grade-dry m7-grade   # 타깃이 선언돼 있는지만 확인(실행하지 않음)
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - freeze가 사람 CSV 해시 미기록·루브릭 해시 불일치·파일럿/probe manifest 없음에서 각각 거부되는가?
   - 형식 오류율 중단이 누적 50호출 이후부터이고 쏠림은 중단이 아니라 보고인가?
   - 캐시 키가 (answer_sha256, rubric_sha256, model_settings_hash, mode, repeat)인가?
   - `m7_grade_scores.json`에 점수·id만 있고 텍스트가 없는가?
   - pilot·grade 완료/정지 시 원본 SHA-256·행 수가 커밋 manifest의 `raw`에 기록되고, 시작 시 대조되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make m7-pilot`·`make m7-grade`·`make m7-freeze`를 실행하지 마라. 이유: 실제 Gemini 호출·과금과 사용자 증빙(사람 CSV)이 선행되어야 한다(ADR-014, M7-X1). 이 step은 `--dry-run`·fake 테스트만 한다.
- freeze 후 루브릭·모델 설정·시드를 바꾸는 경로를 만들지 마라. 이유: κ·H5 결과를 본 뒤 조정하면 사전 등록이 무효가 된다(M7-G4).
- 점수 쏠림을 중단 조건으로 만들지 마라. 이유: 거절·실패를 결정론으로 거른 뒤라 정답이 대부분이고 H5부터 채점해 초반 쏠림이 정상일 수 있다(M7-G5). 정상 채점이 중단된다.
- 일일 한도·크레딧 소진 후 재시도 루프를 만들지 마라. 이유: 정지 후 사용자가 재개한다. 호출 낭비·과금 위험이다.
- 실행 산출물(`grades.jsonl`·`pilot/grades.jsonl`)의 원본 해시·행 수를 manifest에 남기지 않고 끝내지 마라. 이유: 원본이 비커밋이라 나중에 변조·손상을 탐지할 수 없다(FP-053, M6 dev `runs.jsonl`은 해시가 커밋되지 않아 입력 검증이 막혔다).
- `grades.jsonl`의 중복 줄을 그대로 집계하지 마라. 이유: append-only 기록은 최신 줄만 읽어야 점수가 부풀지 않는다(FP-032).
- 진행 출력에 API 키·URL·응답 원문을 넣지 마라. 이유: 비밀·원문 노출 방지(FP-034).
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
