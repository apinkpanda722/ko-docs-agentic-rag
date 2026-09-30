# Step 5: llm-answer-client

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/llm/`(`settings.py`·`anthropic_messages.py`·`anthropic_batch.py`·`models.toml`) — 설정 로딩·재시도·원장 기록 방식
- `src/kodocs/costs.py`(`Price`·`estimate_usd`·`budget_estimate_usd`·`LedgerEntry`·BudgetGuard) — Anthropic 가격 항목 등록 방식과 원장 append
- `src/kodocs/config.py`(`load_settings`·`require_key`), `tests/llm/`·`tests/test_costs.py`의 mock 패턴, `tests/test_architecture.py`
- `pyproject.toml`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

OpenAI 답변 모델 클라이언트를 `llm/`에 만든다. **이 step에서만** 아래 의존성 추가를 허용한다: `uv add openai langchain-openai`(버전은 리졸버가 정한 값을 그대로 두고 `pyproject.toml`·`uv.lock`에 반영). 설치 후 **설치된 실제 버전**의 `langchain_openai.ChatOpenAI` 시그니처·Responses API 사용법(`use_responses_api`, `reasoning`, 토큰 상한 인자 이름, 구조화 출력 방식, 사용량 메타데이터 필드)을 소스와 `help()`로 확인하고 그 결과에 맞춰 구현한다(추측 금지, FP-011).

### 1. 설정 (`llm/models.toml` + `llm/answer_settings.py`)
- `models.toml`에 `[answer.luna]`·`[answer.sol]`을 추가한다(기존 `[synth]`·`[golden_queries]`는 그대로). 필드: `model`(`gpt-6-luna` / `gpt-6.1-sol`), `api = "responses"`, `effort = "low"`, `temperature = ""`(비어 있으면 `null`, **보내지 않음**), `max_output_tokens = 4000`, `request_timeout_s = 30`, `max_attempts = 3`, `max_input_tokens = 272000`.
- `AnswerModelConfig`(frozen dataclass)와 `load_answer_config(name)`: 기존 `GenerationConfig`를 수정하지 말고 같은 스타일로 검증·`record()`·`settings_hash()`를 만든다. `record()`에는 `temperature: null`, `effort`, `api`가 그대로 들어간다.

### 2. 가격·원장 (`costs.py` 확장)
- 모델마다 서비스 4종을 등록한다: `openai.gpt-6-luna.input|cached_input|cache_write|output`, `openai.gpt-6.1-sol.input|cached_input|cache_write|output` (단위 1M 토큰). 단가(USD/1M): luna 0.10/0.01/0.125/0.50, sol 2.00/0.10/2.50/10.00. 출처 URL `https://developers.openai.com/api/docs/pricing`, 확인일 `2026-09-30`. 기존 Anthropic 항목과 같은 방식(정가 + 부가세 10%)·`billing_margin=1.0`.
- 호출 원장 기록: 입력(캐시 아닌 것)·캐시 입력·캐시 쓰기·출력 토큰을 **나눠** 계산한다. reasoning 토큰은 출력 단가로 센다. 실제 SDK 응답(또는 SDK 타입 정의)에서 `output_tokens`가 reasoning 토큰을 **이미 포함**하는지 확인하고, 포함하면 이중 가산하지 않는다(`reasoning_tokens`는 참고 필드로만 기록). 응답에 캐시 쓰기 토큰 필드가 **없으면 0으로 두고 필드 부재를 기록에 남긴다**(0을 추측해 채운 것과 구분).
- **입력 272K 토큰 초과 요청은 dry-run과 실제 호출 양쪽에서 거부한다.** 호출 전 추정은 보수적으로 `문자 수 × 1토큰`(한국어 상한)으로 하고 추정이 272,000을 넘으면 거부한다. 호출 후 응답 usage의 입력 토큰이 272,000을 넘으면 오류로 기록한다.
- 월 상한(`MONTHLY_BUDGET_USD`) 검사는 기존 BudgetGuard 방식대로 **호출 전** 최악 비용(입력 추정 + `max_output_tokens`)으로 한다. 상한을 넘길 요청은 보내지 않는다.

### 3. `llm/openai_chat.py` — `ManagedChat`
- `ManagedChat(config: AnswerModelConfig, settings, *, http_client=None, ledger_path=..., ...)`. 내부에서 `ChatOpenAI`를 `use_responses_api=True`, `reasoning={"effort": config.effort}`, **temperature·top_p를 전달하지 않고**, `max_retries=0`, `timeout=config.request_timeout_s`로 만든다. 이유: SDK 자체 재시도는 타임아웃 등 전송 후 실패도 재전송해 이중 과금한다(FP-035).
- 메서드: `invoke(messages, *, tools=None, response_schema=None, stream=False) -> ChatResult`. `ChatResult`는 응답 메시지(도구 호출 포함), `usage`(입력/캐시 입력/캐시 쓰기/출력/reasoning 토큰), `cost_usd`, `latency_ms`, `ttft_ms`(`stream=True`일 때만 첫 청크 도착까지, 아니면 `None`), `attempts`(시도별 상태·시간), `request_id`를 갖는다. 구조화 출력은 `response_schema`(pydantic 모델)로 요청하고 파싱 실패를 삼키지 말고 `ChatParseError`로 올린다(호출자가 `parse_error` 실패로 센다).
- 재시도 정책: **요청 전송 전 실패(연결 실패·연결 타임아웃)와 429·5xx만** 재시도(백오프, `max_attempts`). 읽기 타임아웃·쓰기 타임아웃·프로토콜 오류 등 전송 후 실패는 재전송하지 않고 `BillingUncertainError`를 올린다. 401·402·403·한도 초과·지속 429는 `FatalLLMError`(실행 전체 중단 신호)다. 예외 메시지·로그에 API 키를 넣지 않는다.
- 모든 호출은 결과와 함께 모델 ID·설정 해시·`temperature: null`·effort를 기록하고 원장에 append한다(CRITICAL: 모델명·생성 설정 기록).
- 스트리밍(`stream=True`) 경로는 설치된 langchain-openai에서 사용량 메타데이터가 제공되는지 실제 확인 후 구현한다. 제공되지 않으면 해당 경로의 usage를 `None`으로 기록하고 이유를 주석·최종 보고에 적는다(추측으로 채우지 않는다).
- 이 모듈이 `openai`·`langchain_openai`를 import하는 **유일한 곳**이다.

### 4. 테스트 (모두 mock, 네트워크 금지)
- `httpx.MockTransport` 등으로 SDK가 보내는 **실제 요청 본문**을 캡처해 `temperature`·`top_p`가 없고 `reasoning.effort == "low"`, `max_output_tokens`, Responses API 엔드포인트임을 검증한다. `max_retries=0`을 검증한다.
- 재시도: 429·5xx 후 성공, 연결 실패 후 성공, 읽기 타임아웃은 1회만 시도하고 `BillingUncertainError`, 401은 `FatalLLMError`.
- 비용: 입력·캐시 입력·캐시 쓰기·출력을 나눈 계산(두 모델), reasoning 이중 가산 없음, VAT 반영, 월 상한 초과 시 호출 전 거부, 272K 초과 거부(dry-run·실행 둘 다).
- 계층 가드(`tests/test_architecture.py`): `kodocs.agent`·`kodocs.mcp_server`·`kodocs.tools`는 `openai`·`langchain_openai`를 import하지 않는다. 위반 fixture로 검사기를 테스트한다.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/llm tests/test_costs.py tests/test_architecture.py
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - 요청 본문에 temperature·top_p가 없고 SDK 재시도가 꺼져 있는가(실제 캡처로 확인)?
   - 가격·확인일·출처가 확정값과 일치하는가(luna 0.10/0.01/0.125/0.50, sol 2.00/0.10/2.50/10.00)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- SDK 기본 재시도(`max_retries`)를 켜두지 마라. 이유: 전송 후 타임아웃을 재전송하면 서버가 이미 처리·과금했을 수 있다(FP-035).
- temperature·top_p를 0이나 기본값으로라도 전송하지 마라. 이유: effort가 none이 아니면 미지원이며 400 오류가 난다. 기록은 `null`이다.
- 응답 usage 필드 이름을 문서·기억으로 추측하지 마라. 이유: 실제 SDK 응답·타입 정의로 확인한다(FP-011, M2에서 응답 키를 추측해 기록이 비었다).
- reasoning 토큰을 출력 토큰에 다시 더하지 마라. 이유: 출력 토큰에 이미 포함되어 있으면 비용이 이중 계산된다. 실제 응답 구조로 확인한다.
- 이 step 외 `uv add`를 하거나 `openai`·`langchain_openai` 이외 패키지(langgraph·mcp)를 추가하지 마라. 이유: 의존성은 승인된 step에서만 추가한다.
- 실제 OpenAI 호출·`OPENAI_API_KEY` 사용·`.env` 열람을 하지 마라. 이유: 테스트는 mock이며 실제 호출은 사용자가 실행한다(ADR-014). 테스트는 명시적 env 매핑을 주입한다.
- `agent/`·`tools/`에서 직접 `ChatOpenAI`를 만들지 마라. 이유: 외부 LLM 호출은 `llm/` 래퍼로만 한다(CRITICAL).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
