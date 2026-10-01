# Step 2: gemini-client

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `src/kodocs/llm/`(`settings.py`·`answer_settings.py`·`openai_chat.py`·`models.toml`) — 설정 로딩·재시도 분류·원장 기록·예외 계층 방식. `openai_chat.py`의 `ManagedChat`이 가장 가까운 선례다
- `src/kodocs/costs.py`(`Price`·`estimate_usd`·`budget_estimate_usd`·`LedgerEntry`·`CostLedger`·`BudgetGuard`)와 `tests/test_costs.py`, `tests/llm/`의 mock 패턴, `tests/test_architecture.py`(계층 가드)
- `src/kodocs/config.py` — `GEMINI_API_KEY`가 이미 필수 키 목록에 있다. `load_settings`·`require_key` 사용법
- `pyproject.toml`·`uv.lock` — **사용자가 `uv add google-genai`를 이미 실행했다고 가정한다**
- `docs/results/hypotheses.md`의 M7-X2·M7-G4·M7-G5 (429 분류·단가·설정 규칙)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

Gemini 채점 클라이언트를 `llm/`에 만든다. 이 모듈이 `google.genai`를 import하는 **유일한 곳**이다.

### 0. 선행 조건 확인
- `uv run python -c "import google.genai"`가 성공하는지 확인한다. 실패하면(미설치) **설치하지 말고** `blocked`로 보고한다(`reason`: 사용자가 `uv add google-genai`를 실행해야 한다). 이 step이 의존성을 추가하지 않는다(FP-040).
- 설치된 **실제 버전**의 SDK 소스·타입을 읽고 아래를 확인한 뒤 그 결과에 맞춰 구현한다(추측 금지, FP-011): 구조화 출력(JSON 스키마/`response_schema`) 방식, thinking 설정 인자 이름과 모델이 지원하는 가장 낮은 수준의 지정법, `temperature`·`seed` 인자, 사용량 필드(입력·후보 출력·thinking 토큰 필드와 총계 관계), 요청 ID·모델 버전 필드, **오류 예외 타입과 HTTP 상태·본문 구조**(특히 429의 상세: quota id/metric 위치). 확인한 SDK 버전과 소스 경로를 최종 보고에 적는다. 확인이 안 되는 항목은 "확인 불가"라고 보고한다(추측으로 채우지 않는다).

### 1. 설정 (`llm/models.toml` + 로더)
- `[grade.gemini]`를 추가한다(기존 항목은 그대로): `model = "gemini-3.8-flash"`, `tier = "paid"`, `thinking_level = ""`(비어 있으면 `null`; m7-probe 결과로 확정, freeze 대상), `temperature = ""`, `seed = ""`(비어 있으면 `null`·**보내지 않음**), `max_output_tokens = 4000`(thinking 포함 호출 상한), `request_timeout_s = 60`, `max_attempts = 3`, `rpm_pacing = 10`(**자체 보수값이며 공식 한도가 아니다**, 주석으로 명시), `rpm = ""`·`tpm = ""`·`rpd = ""`(유료 등급 공식 한도, 사용자가 확인해 알려 줄 때까지 `null`).
- `GradeModelConfig`(frozen dataclass), `load_grade_config()`: 기존 `AnswerModelConfig` 스타일로 검증·`record()`·`settings_hash()`를 만든다. `record()`에는 모델 ID·tier·thinking·temperature·seed(null 가능)·`rpm_pacing`이 들어간다. 모델 ID가 비어 있으면 `ConfigError`로 호출을 거부한다.

### 2. 가격·유효기간·원장 (`costs.py` 확장, 기존 동작 불변)
- `Price`에 `valid_until: str | None = None`(ISO 날짜)을 추가한다(기본 None이라 기존 단가에 영향 없음).
- 서비스 등록: `google.gemini-3.8-flash.input`(USD 0.75 / 1M 토큰)·`google.gemini-3.8-flash.output`(USD 3.75 / 1M 토큰, thinking 포함). 출처 URL `https://ai.google.dev/gemini-api/docs/pricing`, 확인일 `2026-10-01`, `valid_until="2026-12-31"`, 기존 항목과 같은 방식(정가 + 부가세 10%), `billing_margin=1.0`. 2027-01-01부터 입력 $1.50·출력 $7.50이라는 사실은 **주석으로만** 남기고 활성 단가로 등록하지 않는다.
- `ensure_price_valid(service, *, on: date | None = None)`을 추가한다: `on`(기본 오늘, KST 기준)이 `valid_until`보다 늦으면 `PriceExpiredError`를 올린다. `GeminiGrader`는 **모든 호출 전과 dry-run에서** 호출한다.
- `LedgerEntry`에 선택 필드 `tier: str | None = None`을 추가하되, **값이 있을 때만 직렬화한다**(기존 원장 줄 형식·기존 테스트가 바뀌지 않아야 한다). Gemini 호출은 `tier="paid"`로 기록하고 월 상한 검사(BudgetGuard)에 포함한다. 입력·출력 토큰을 나눠 기록하고 thinking 토큰이 출력에 이미 포함되는지는 SDK 사용량 필드로 확인해 이중 가산하지 않는다.

### 3. `llm/gemini_chat.py` — `GeminiGrader`
- `@dataclass(frozen=True) GradeRequest(item_id: str, system_prompt: str, user_content: str, schema: type[BaseModel])`, `GradeResult(parsed, usage, cost_usd, latency_ms, attempts, request_id, model_record)`.
- `GeminiGrader(config, settings, *, client=None, ledger_path=..., clock=time.monotonic, sleep=time.sleep)`: `grade(request) -> GradeResult`, `estimate(request) -> GradeCostEstimate`(입력 추정 토큰·최악 비용 = 입력 추정 + `max_output_tokens`).
- **RPM 페이싱**: 요청 시작 간격을 `60 / rpm_pacing`초 이상으로 유지한다(주입한 `clock`·`sleep`로 테스트).
- **재시도 정책**(FP-035): 요청 전송 전 실패(연결 실패·연결 타임아웃)와 5xx와 *분당 한도 429*만 재시도한다(지수 백오프, `max_attempts`). 읽기·쓰기 타임아웃 등 전송 후 실패는 재전송하지 않고 `BillingUncertainError`를 올린다(기록은 `billing_uncertain`). 구조화 출력 파싱·범위(0~3) 검증 실패는 `GradeParseError`다(재시도하지 않고 호출자가 형식 오류로 센다).
- **429 분류**(할당량 상세(quota id/metric)와 에러 본문으로 분류, SDK에서 확인한 구조 사용):
  - 분당 한도(RPM·TPM): 지수 백오프 후 재시도한다. 연결 전 실패로 보므로 `billing_uncertain`이 아니다.
  - 일일 한도(RPD): **즉시 정지**한다. `DailyQuotaExhausted`를 올리고 메시지에 남은 건수(호출자가 채워 넣는 필드)와 다음 초기화 시각(태평양시 자정을 `zoneinfo`로 KST 환산, 서머타임 반영)을 담는다.
  - 선불 크레딧 소진: **즉시 정지**하고 재시도하지 않는다. `CreditExhausted`. 남은 건수를 메시지에 담는다.
  - 분류할 수 없는 429: 보수적으로 **정지**한다(`UnclassifiedRateLimit`).
  - 위 정지 예외는 공통 기반 `GradeStopError`를 상속하고 `remaining: int | None` 속성을 갖는다. 정지는 재개 가능해야 한다(호출자가 캐시로 이어 간다).
  - 401·402·403은 `FatalLLMError`(실행 중단)다. 예외 메시지·로그에 API 키를 넣지 않는다.
- **429 원본 응답**은 로컬 로그 `data/interim/m7/gemini_429.jsonl`(append, gitignore 대상)에 남긴다: 시각·상태·본문·분류 결과. **헤더·URL의 키는 제거한다**(API 키 문자열이 로그에 없다는 테스트).
- 모든 호출은 모델 ID·설정 해시·thinking·temperature·seed(null 가능)를 결과와 원장에 기록한다. 요청에는 설정된(null이 아닌) 값만 보낸다.

### 4. `make m7-probe` (선언 + 구현, 실행은 사용자)
- `src/kodocs/llm/gemini_probe.py`(`python -m kodocs.llm.gemini_probe [--dry-run]`)와 Makefile 타깃 `m7-probe`, `.PHONY`, `AGENTS.md` 명령어 항목(`make m7-probe  # Gemini 채점 모델 설정 확인(1~3회 실제 호출·과금) — 사용자 실행`).
- 최대 3회 호출: (1) 기본(thinking 최저 수준) 구조화 채점 더미 요청 (2) temperature 지정 (3) seed 지정. 각 호출의 수락/거절과 오류 타입·상태, 사용량 필드 이름, thinking 토큰 사용 여부, 응답에 보고된 모델 버전, 파싱 성공 여부를 `data/manifest/m7_probe.json`에 기록한다(API 키·응답 원문 없음, 점수 정수만). `--dry-run`은 호출 계획과 예상 비용만 출력한다. 실행 전 `ensure_price_valid`와 월 상한 검사를 한다.
- thinking 지원 수준은 문서·SDK 타입으로 확인한 근거와 실제 응답 결과를 함께 기록해, 사용자가 `models.toml`의 `thinking_level`·`temperature`·`seed`를 확정할 수 있게 한다. **이 step이 직접 probe를 실행하지 않는다.**

### 5. 계층 가드·테스트 (모두 mock, 네트워크 금지)
- `tests/test_architecture.py`: `kodocs.agent`·`kodocs.tools`·`kodocs.retrieval`·`kodocs.mcp_server`가 `google.genai`·`google`을 import하지 않는다. 위반 fixture로 검사기를 테스트한다.
- 429 분류 fixture는 **설치된 SDK가 실제로 던지는 예외 타입과 에러 본문 구조**로 만든다(SDK 클래스를 직접 생성). 분당 한도→백오프 후 성공, 일일 한도→즉시 정지(재시도 0회, 다음 초기화 KST 시각 포함), 크레딧 소진→즉시 정지, 분류 불가→정지.
- 읽기 타임아웃 1회 시도 후 `BillingUncertainError`, 401 `FatalLLMError`, 파싱 실패 `GradeParseError`, 페이싱 간격, `PriceExpiredError`(`on`을 2027-01-01로 주입), 비용 계산(입력·출력 분리, VAT 반영, thinking 이중 가산 없음), 원장 `tier=paid` 기록과 기존 원장 줄 형식 불변, 429 로그에 API 키 부재, 월 상한 초과 시 호출 전 거부를 테스트한다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/llm tests/test_costs.py tests/test_architecture.py
uv run python -m kodocs.llm.gemini_probe --dry-run   # 네트워크 없음, 호출 계획·예상 비용만
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - `models.toml`의 `rpm_pacing=10`이 '자체 보수값(공식 한도 아님)'으로 주석 처리되고 `rpm`·`tpm`·`rpd`가 null인가?
   - `valid_until` 이후 `ensure_price_valid`가 거부하고, 2027-01-01 단가는 활성 단가로 등록되지 않았는가?
   - 429 fixture가 SDK 실제 예외 타입으로 만들어졌는가? 확인하지 못한 항목을 최종 보고에 적었는가?
   - 기존 원장 줄 형식과 기존 `tests/test_costs.py`가 바뀌지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `uv add`·`pip install`로 SDK를 설치하지 마라. 이유: 사용자가 직접 설치하기로 했다(FP-040). 미설치면 `blocked`로 보고한다.
- SDK의 예외 타입·429 본문 구조·사용량 필드 이름을 문서나 기억으로 추측하지 마라. 이유: 실제 SDK 소스로 확인한다(FP-011). 확인 못 한 항목은 '확인 불가'로 보고한다.
- 일일 한도·크레딧 소진·분류 불가 429를 재시도하지 마라. 이유: 재시도해도 풀리지 않고 호출만 낭비한다. 정지 후 사용자가 재개한다.
- 읽기·쓰기 타임아웃 등 전송 후 실패를 재전송하지 마라. 이유: 서버가 이미 처리·과금했을 수 있다(FP-035).
- `make m7-probe`를 실행하지 마라. 이유: 실제 Gemini 호출·과금은 사용자가 실행한다(ADR-014). 이 step은 `--dry-run`만 실행한다.
- 공식 한도(RPM·TPM·RPD)를 추측해 `models.toml`에 넣지 마라. 이유: 사용자가 확인해 알려 줄 값이다. `rpm_pacing=10`은 자체 페이싱 값이다.
- `agent/`·`tools/`·`retrieval/`에서 Gemini SDK를 import하지 마라. 이유: 외부 LLM 호출은 `llm/` 래퍼로만 한다(CRITICAL).
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
