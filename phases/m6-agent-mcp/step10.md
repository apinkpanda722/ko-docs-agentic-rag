# Step 10: m6-runner

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/agent/`(`answer.py`·`types.py`·`guardrails.py`·`trace.py`), `src/kodocs/llm/openai_chat.py`·`answer_settings.py`·`models.toml`, `src/kodocs/costs.py`, `src/kodocs/tools/`(`build_tool_deps`·`load_tool_config`)
- M5 실행기 패턴: `src/kodocs/eval/m5_test.py`(test 잠금·재실행 사유·실행 기록), `m5_dev.py`, `m5_runner.py`, `m5_inputs.py`, `m5_stats.py`(`paired_bootstrap`·`describe`·`verdict`), `m5_report.py`(생성 영역 패턴), `data/manifest/m5_config.json`·`m5_test_runs.jsonl`
- `data/golden/queries/gold.jsonl`·`silver.jsonl`(필드: `id`·`set`·`split`·`type`·`source`·`asker`·`expected_response`·`answer_values`·`qrels`·`tags`) — 읽기만, `data/synthetic/canaries.json`·`data/synthetic/docs/`(access 메타데이터) — 읽기만
- `src/kodocs/eval/text_normalize.py`, `src/kodocs/eval/golden_specs.py`의 `answer_values` 정규화 함수(NFC·공백·마침표, ADR-025 — **새로 만들지 말고 기존 함수를 찾아 재사용**)
- `Makefile`, `tests/test_m5_make_targets.py`, `tests/test_m6_make_targets.py`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

M6 실행기(dev 모델 비교 · 고정 · test 실행 · dry-run)를 만든다. **이 step에서는 실제 API를 호출하지 않는다.** 실행 명령은 사용자가 돌린다. 모듈 이름은 `src/kodocs/eval/m6_*.py`(M5의 `m5_` 규칙과 같음).

### 1. `m6_inputs.py` — `make m6-inputs` (네트워크 없음)
`data/manifest/m6_eval_inputs.json`에 (문항 ID·개수·해시만, 질문 원문 없음) 기록한다.
- `dev_set`: 골드 dev 20 + 실버 dev 55 = 75문항 ID.
- `h5_pool`: 골드 test identifier 4 + sql 4 + multi_doc 5 = 13문항 ID(개수가 13이 아니면 실패).
- `answer_match_denominator`: `split=dev ∧ expected_response=answer ∧ answer_values 비어있지 않음`(현재 골드 dev 13[실제 3·합성 10] + 실버 dev 43 = 56; **출처별 개수**를 함께 기록하고, 56이 아니면 실패로 멈춘다). 이 집합에서 빠진 dev 문항 ID와 사유(`answer_values` 없음·`expected_response` 아님)를 `excluded`로 기록한다(FP-021, FP-043: 무엇을 세는지 명시).
- `gate_permission_dev`: dev `type=permission` 8문항(골드 2 + 실버 6). `unanswerable_dev`: dev unanswerable 8문항.
- `router_types`: dev에서 `expected_response=answer`이고 문항이 8개 이상인 유형과 문항 수(현재 identifier 13, multi_doc 12, semantic 20, temporal 10). 8개 미만 유형(sql·table)은 `below_threshold`로 기록.
- 골드·실버 파일 SHA-256, `m5_config.json`의 `config_hash`.

### 2. `m6_metrics.py` — 결정론 지표 (Gemini 없음)
- `answer_values_match(answer, answer_values) -> bool`: `answer_values` **전부**가 ADR-025 정규화 후 답변에 포함되어야 참. 기존 정규화 함수를 재사용한다.
- `citation_precision(result, qrels)`: 인용한 `doc` 인용 중 qrels에서 rel≥1인 문서의 비율. 문서 인용이 없거나(거절·`sql` 인용만) qrels가 비어 있는 문항은 분모에서 빼고 개수를 함께 보고한다.
- `refusal_rate`: unanswerable 문항의 `refused=true` 비율(기술 통계). permission 문항은 `refused=true`이고 답이 `NO_ANSWER_TEXT`와 같은 비율(기술 통계).
- `leak_check_ab(result, asker, docs_meta, canaries) -> dict`: (a) 이 문항의 **모든 도구 결과 refs**에 질문자가 볼 수 없는 제한 문서 ID(`can_read_doc`로 판정)가 있으면 참, (b) 답변에 질문자가 볼 수 없는 제한 문서의 카나리아 문자열이 있으면 참. (c)는 구현하지 않는다(M7). 게이트는 dev permission 8문항의 (a)·(b) 합이 1건 이상이면 탈락.
- 집계 함수: 시스템별·모델별 비용·토큰·단계 수·지연 P50, 가드레일 종료 수, 상태(`completed`·`billing_uncertain`·`infra_error`) 수. **분자·분모는 같은 대상 집합에서 구한다**(FP-036). 임베더 로딩(`init_s`)은 문항별 지연 집계에서 제외하고 따로 기록한다.

### 3. `m6_runner.py` — 공통 실행 루프
- 입력: 문항 목록, 시스템 목록(`simple`,`agentic`), 모델 설정, `AgentRuntime` 팩토리, 동시성(dev·test 기본 4, 기록), 출력 경로.
- 각 (문항, 시스템, 모델)을 `answer(...)`로 실행하고 결과를 **append-only JSONL**(`data/interim/m6/.../runs.jsonl`, 비커밋)에 한 줄씩 쓴다: `question_id`, `system`, `model`, `run_seq`, `status`(`completed`·`billing_uncertain`·`infra_error`), `answer`, `citations`, `refused`, `refuse_reason`, 토큰·비용·지연·단계 수, `trace_id`, `attempts`.
- **캐시·재개 키(FP-033)**: (문항 ID, 시스템, 모델 설정 해시, 공통·Agentic 프롬프트 해시, 가드레일 해시, M5 config_hash, run_seq). 같은 키의 `completed`·`billing_uncertain` 줄이 있으면 다시 호출하지 않는다(재과금 방지). `infra_error`(재시도 소진된 연결 실패)만 재실행 시 다시 시도하고 그 횟수를 기록한다.
- 실패 처리: 문항 단위 예외는 `status`와 함께 기록하고 **계속 진행한다**(FP-030). `BillingUncertainError` → `billing_uncertain`(재시도 없음, 실패로 셈). `FatalLLMError`(인증·결제·한도) → 즉시 전체 중단. 예산 상한 초과 예정이면 호출 전에 멈춘다(규칙 22).
- **진행 상황을 N건마다 `flush=True`로 출력**한다(FP-034). 진행 줄에 키·토큰·질문 원문을 넣지 않는다.
- 임베더(BGE-M3)는 한 번만 로딩하고 시간은 `init_s`로 따로 기록하며, 스레드에서 임베딩 호출이 안전하지 않으면 잠금으로 직렬화한다. 시작 시 M5 `config_hash`가 `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`가 아니면 실행을 거부한다.
- 실제 `ManagedChat`·`build_tool_deps()`를 만드는 코드는 CLI `main()` 안에서만 만든다(테스트는 fake 주입).

### 4. `m6_dev.py` — `make m6-dry`, `make m6-dev`
- **dry-run (`--dry-run`, 네트워크 없음)**: 2모델 × 2시스템 × 75문항 = 300회의 예상 토큰·비용을 출력하고 `data/manifest/m6_dry_runs.jsonl`에 `mode="sync"`, 가정, 단가 확인일, 동시성과 함께 append한다. (a) **예상**: 단순 RAG는 모델 호출 1회(입력 = 프롬프트 + 질문 + 6,000자 근거, 출력 1,000토큰 가정), Agentic은 도구 턴 3회 + 최종 1회로 가정, (b) **상한**: 문항당 토큰 상한 100,000을 전부 입력 단가로 + `max_output_tokens` 4,000을 출력 단가로 계산. 입력 추정은 `문자 수 × 1토큰`(보수적)이며 272K 초과 요청은 거부한다. 예상 비용 × `billing_margin`이 남은 월 예산을 넘으면 실행을 거부한다. 가정 수치를 출력·기록한다.
- **실행 (`make m6-dev`, 실제 OpenAI 호출·과금, 사용자 실행)**: `--limit N`(`LIMIT=N`) 파일럿은 별도 경로(`data/interim/m6/pilot/`)에 쓰고 선택에 쓰지 않는다. **파일럿 `runs.jsonl`과 `data/manifest/m6_pilot.json`(ID·수치만)에는 가드레일 종료 사유별(`guardrail_max_steps`·`guardrail_tokens`·`guardrail_timeout`) 건수(시스템별)와 문항별 누적 토큰 최댓값(시스템별)을 기록하고 콘솔에도 출력한다**(dev 비교 전 가드레일 1회 조정 판단 근거, 규칙 8). 본 실행 `m6_dev.json`에도 같은 집계를 넣는다. 본 실행은 75문항 × 2시스템 × 2모델을 실행해 `data/interim/m6/dev/runs.jsonl`에 쓴 뒤, 규칙 11~13으로 **선택 판정**을 계산해 `data/manifest/m6_dev.json`에 쓴다(문항 텍스트·답변 텍스트 없이 ID·수치·해시): 모델별·시스템별 정답 값 포함률(분모 56, 출처별 분리), 게이트 (a)(b) 결과, unanswerable 거절률, 비용·지연 P50·동시성, 두 시스템 평균 정답률의 문항 짝 비교 부트스트랩 차이·95% CI(`m5_stats.paired_bootstrap`, 10,000회, 시드 20260930), 선택 결과와 그 사유(`gate_failed` / `ci_excludes_zero` / `cheaper_on_tie` / `latency_on_cost_tie`), 입력·설정·프롬프트·가드레일 해시, 코드 커밋.
- 선택 규칙 구현(규칙 12·13): 게이트 통과 모델만 후보 → 후보 하나면 그 모델, 둘이면 CI가 0을 배제할 때 정답률이 높은 모델, 아니면 문항당 평균 비용(두 시스템 평균)이 낮은 모델, 비용이 같으면 P50 지연이 짧은 모델(두 모델이 **같은 동시성**으로 돈 경우에만). **둘 다 게이트 탈락이면 자동 선택하지 않고 `selection: null`과 사유를 남기고 사용자 결정을 기다린다.**
- dev 답변(선택 모델·양 시스템)은 M7의 라우터 착수 조건 판정(규칙 23)과 Gemini 채점 입력이 되므로 `runs.jsonl`을 그대로 보존한다(추가 실행 없음).

### 5. `m6_config.py` — `make m6-freeze` (사용자 실행)
- `data/manifest/m6_dev.json`의 선택 결과(모델 ID, `answer.*` 설정 기록·해시, 공통·Agentic 프롬프트 해시, 가드레일 설정 해시·숫자, M5 `config_hash`, 코드 커밋, `frozen_at`)를 `data/manifest/m6_config.json`에 쓰고 `config_hash`를 계산한다. 파일이 이미 있으면 **덮어쓰기를 거부**한다. `selection`이 `null`이면 거부한다. 프롬프트·가드레일 해시가 dev 실행 기록과 다르면(비교 뒤 변경, 규칙 14) 거부한다.

### 6. `m6_test.py` — `make m6-test` (실제 API·과금, 사용자 실행)
- `m6_config.json`이 없으면 거부한다. 고정된 모델·프롬프트·가드레일이 현재 코드의 해시와 다르면 거부한다. 골드 test 30문항 × 2시스템을 **1회씩** 실행한다(동시성 4, 기록).
- 잠금·재실행은 `m5_test.py`와 같은 규칙이다: 성공한 실행이 있으면 재실행은 `RERUN_REASON="사유"`가 있어야 하고, 사유·날짜·이전 결과 해시를 `data/manifest/m6_test_runs.jsonl`에 누적한다. 실패한 시도는 잠금을 소모하지 않는다.
- 산출물: `data/interim/m6/test/run-NN/runs.jsonl`(비커밋, M7 Gemini 채점 입력이며 `h5_pool` 여부 플래그 포함), `data/manifest/m6_test_results/run-NN.json`(ID·수치·해시만: 결정론 지표, 비용, 지연 P50, 단계 수, 가드레일·`billing_uncertain` 수).
- 지연 P95 판정은 하지 않는다(규칙 21: 동시성 1 별도 실행은 M7).

### 7. Makefile·AGENTS.md
`Makefile`에 `m6-inputs`, `m6-dry`, `m6-dev`, `m6-freeze`, `m6-test` 타깃(`verify`에는 넣지 않음, `m6-dev`는 `LIMIT` 변수, `m6-test`는 `RERUN_REASON` 변수를 `m5-test`처럼 전달)을 추가하고 `AGENTS.md` 명령어 목록에 다음을 추가한다.
`make m6-inputs    # M6 평가 입력 집계(dev 75·H5 풀 13·정답 값 분모 56·게이트·라우터 유형) → data/manifest/m6_eval_inputs.json (네트워크 없음)` /
`make m6-dry       # dev 비교 예상 토큰·비용(2모델×2시스템×75문항, 예상·상한) — 네트워크 없음` /
`make m6-dev       # 답변 모델 dev 비교(LIMIT=N 파일럿 가능) — 실제 OpenAI 호출·과금, 사용자 실행` /
`make m6-freeze    # 선택 모델·프롬프트·가드레일을 data/manifest/m6_config.json에 고정(덮어쓰기 거부) — 사용자 실행` /
`make m6-test      # 골드 test 30문항×두 시스템 1회 실행(재실행은 RERUN_REASON="사유" make m6-test) — 실제 OpenAI 호출·과금, 사용자 실행`. `tests/test_m6_make_targets.py`에 선언 테스트를 추가한다.

### 8. 테스트 (fake `answer`·fake 임베더, 네트워크·API 없음)
- 입력 집계: 실제 골드·실버 파일로 dev 75·H5 풀 13·분모 56(골드 13[실제 3·합성 10]·실버 43)·permission dev 8·unanswerable dev 8·라우터 유형 4개가 맞고, 개수가 어긋나면 실패하는 케이스도 fixture로 검증.
- 지표: 정답 값 전부 포함 판정(일부만 포함은 거짓, 정규화), 인용 정밀도 분모 제외, 누설 (a)·(b), 분자·분모 집합 일치.
- 선택 규칙: 게이트 탈락, CI가 0 배제, 0 포함 시 비용, 비용 동률 시 지연, 지연 동시성 불일치 시 지연 사용 안 함, 둘 다 탈락 시 `selection: null`.
- 러너: 캐시 재사용(같은 키 재호출 없음), `infra_error`만 재시도, 문항 하나 실패해도 계속, `FatalLLMError`는 전체 중단, 예산 초과 예정 시 호출 전 중단, M5 해시 불일치 거부, 진행 출력 flush, 평문 키·토큰이 출력·기록에 없음.
- 고정·test: `m6-freeze` 덮어쓰기 거부·해시 불일치 거부, `m6-test` 설정 없음 거부·재실행 사유 필수·실패 시도가 잠금을 소모하지 않음.
- 규칙 8·9의 숫자와 모델 설정이 `models.toml`·가드레일 기본값과 일치.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/eval -k m6
make m6-inputs   # 로컬 산출물이 있으면 실행 가능, 없으면 사유를 최종 보고에 적는다(네트워크·API 없음)
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - dev 비교가 모델 하나만 바꾸고(설정·프롬프트·가드레일 동일) 두 시스템 모두에서 실행되는가?
   - 선택·게이트·분모 정의가 hypotheses.md 규칙 9~14와 글자 그대로 일치하는가(분모 56, 게이트 (a)(b), 동시성 조건)?
   - 실제 API를 호출하는 코드가 `main()` 밖에서 실행되지 않고 테스트는 전부 fake인가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make m6-dev`·`m6-test`·`m6-dry`(실행 부분)를 실제로 실행하거나 OpenAI를 호출하지 마라. 이유: 실제 API 호출은 사용자가 직접 실행한다(ADR-014, AGENTS.md).
- `make m5-test`·`make m5-*` 실행이나 `data/manifest/m5_*` 수정을 하지 마라. 이유: M5 결과·잠금은 소급 변경하지 않는다(규칙 16).
- dev·test 결과를 보고 규칙(정답 값 포함 정의·게이트·선택 순서·분모)을 코드에서 바꾸지 마라. 이유: 결과를 보기 전에 고정한 사전 등록 규칙이다(ADR-026). 필요하면 hypotheses.md 변경 기록에 사유를 남겨야 하며 이 step에서 규칙 변경은 `blocked` 사유다.
- 선택 판정을 사람에게 묻지 않고 두 모델 모두 게이트 탈락인데 임의로 하나를 고르지 마라. 이유: 규칙 12는 그 경우 사용자 결정을 기다린다.
- 실패한 문항(가드레일·`billing_uncertain`·파싱 실패)을 지표 분모에서 빼거나 다시 돌려 성공한 값으로 대체하지 마라. 이유: 규칙 6은 실패를 제외하지 않고 실패로 센다. 재시도는 연결 실패·429·5xx에 한하고 기록한다.
- 답변 텍스트·질문 원문·실제 규정 조문을 `data/manifest/`나 커밋 대상에 쓰지 마라. 이유: 원문은 `data/interim/` 로컬 전용이며 manifest에는 ID·수치·해시만 남긴다(ADR-002).
- 골드·실버의 `answer_values`를 채우거나 수정하지 마라. 이유: `data/golden/`은 사람이 확정한 기준이다. 분모에서 빠지는 문항은 `excluded`로만 기록한다(FP-021).
- 분모·분자를 다른 집합에서 구하지 마라(예: 실패 문항 시간은 넣고 쪽수·문항은 뺌). 이유: FP-036에서 sec/page가 분자·분모 대상 집합 불일치로 왜곡됐다.
- 재시도 소진 후 조용히 성공 처리하거나 `max_retries`를 켜지 마라. 이유: 전송 후 실패 재시도는 이중 과금이다(FP-035).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
