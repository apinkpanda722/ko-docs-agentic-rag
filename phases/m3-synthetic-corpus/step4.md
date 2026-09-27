# Step 4: llm-batch

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL: 외부 LLM 호출은 `src/kodocs/llm/` 래퍼로만, 샘플링 파라미터는 모델이 지원할 때만 설정·기록(미지원이면 `null` + thinking·effort 기록), 실제 API 스크립트는 누적 비용 기록·상한 전 중단, `.env` 금지
- `docs/ADR.md` — ADR-003(보완), ADR-004, ADR-014, ADR-023(원장·`billing_margin`), **ADR-024**
- `src/kodocs/costs.py` — `Price`, `PRICES`, `estimate_usd`, `budget_estimate_usd`, `CostLedger.record`, `BudgetGuard.check`
- `src/kodocs/config.py` — `load_settings()`, `require_key("ANTHROPIC_API_KEY")`
- `src/kodocs/ingest/manifest.py` — `append_record`, `load_records`, `latest_records`
- `src/kodocs/parsing/upstage_adapter.py`, `tests/parsing/test_upstage_adapter.py` — M2 과금 API 어댑터의 원장·재시도·키 비노출 패턴(참고만, 수정 금지)
- `tests/test_costs.py`

## 배경 — 공식 문서로 확인한 사양 (2026-09-27 확인. 네트워크로 다시 확인하지 마라)

**가격** — https://platform.claude.com/docs/en/about-claude/pricing (확인일 2026-09-27), 모델 `claude-sonnet-5`, 단위 USD/MTok:

| 항목 | 일반 | Batch(50%) |
|---|---|---|
| 입력(base input) | 2.00 | 1.00 |
| 출력 | 10.00 | 5.00 |
| 5분 캐시 쓰기 | 2.50 | 1.25 |
| 1시간 캐시 쓰기 | 4.00 | 2.00 |
| 캐시 적중·갱신 | 0.20 | 0.10 |

- 공식 문서 인용: "These multipliers stack with other pricing modifiers, including the Batch API discount". Batch 캐시 단가는 이 문장에 근거해 일반 캐시 단가 × 0.5로 계산한 값이다(주석에 적는다).
- Sonnet 5는 샘플링 파라미터(`temperature`·`top_p`·`top_k`)를 기본값이 아닌 값으로 보내면 400을 반환하고, `thinking: {"type": "disabled"}`는 허용한다. 샘플링 seed 파라미터는 없다.

**Message Batches** — https://platform.claude.com/docs/en/build-with-claude/batch-processing (확인일 2026-09-27):

- 배치당 최대 100,000건 또는 256MB.
- "Batches expire if processing does not complete within 24 hours." 결과는 모두 끝나거나 24시간이 지나면 받을 수 있다.
- "Batch results are available for 29 days after creation."
- "All usage is charged at 50% of the standard API prices."
- 결과 유형 4가지: `succeeded`(메시지 포함), `errored`("You will not be billed for these requests"), `canceled`(동일, 미과금), `expired`(24시간 만료 전에 모델로 보내지지 못함, 미과금).
- "Batch results can be returned in any order" → **반드시 `custom_id`로 매칭**한다.
- 프롬프트 캐싱은 Batch에서 best-effort다(이번 phase는 캐싱을 쓰지 않는다. 단가만 등록한다).
- `custom_id`: https://platform.claude.com/docs/en/api/messages/batches/create — `minLength 1, maxLength 64, pattern ^[a-zA-Z0-9_-]{1,64}$`.
- `processing_status`: `in_progress` | `canceling` | `ended`.

**Python SDK** — `anthropic` 1.x(2026-09-27 PyPI 최신 1.8.0). 문서화된 호출만 쓴다:
- `client.messages.batches.create(requests=[Request(custom_id=..., params=MessageCreateParamsNonStreaming(...))])` (`from anthropic.types.message_create_params import MessageCreateParamsNonStreaming`, `from anthropic.types.messages.batch_create_params import Request`)
- `client.messages.batches.retrieve(batch_id)` → `.id`, `.processing_status`, `.request_counts.{processing,succeeded,errored,canceled,expired}`, `.created_at`, `.expires_at`, `.ended_at`
- `client.messages.batches.results(batch_id)` → 반복 가능 객체. 각 항목 `.custom_id`, `.result.type`, `succeeded`면 `.result.message`(`.model`, `.content[]`(블록 `.type == "text"`면 `.text`), `.stop_reason`, `.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`), `errored`면 `.result.error`
- `client.messages.batches.list(limit=...)` (자동 페이지 넘김)
- 예외: `anthropic.APIStatusError`(하위 `BadRequestError` 400, `AuthenticationError` 401, `PermissionDeniedError` 403, `NotFoundError` 404, `RateLimitError` 429, `InternalServerError` ≥500 등, `.status_code`), `anthropic.APIConnectionError`(하위 `APITimeoutError`)
- SDK는 기본으로 연결 오류·408·409·429·5xx를 **자동 재시도**한다(`max_retries` 기본 2). 1.x는 `httpx`가 아니라 `httpx2` 기반이다.

## 작업

TDD: `tests/llm/test_anthropic_batch.py`와 `tests/test_costs.py` 추가분을 먼저 작성한다. SDK 클라이언트는 **위 문서 형태를 그대로 흉내 낸 가짜 객체**(`fake.messages.batches.create/retrieve/results/list`)를 `client_factory`로 주입한다. 실제 SDK 객체·네트워크를 쓰지 않는다.

### 1. 의존성

- `uv add "anthropic>=1.8.0,<2"`(메인 의존성), `uv.lock` 갱신. SDK 내부 모듈(`anthropic._*`)을 import하지 않는다.

### 2. 가격 등록 — `src/kodocs/costs.py`

- `PRICES`에 7개 서비스 추가(단위 `"token"`, `unit_price_usd`는 토큰당 USD, 예: 1.00/MTok → `1e-06`). 모두 `source_url`·`checked_on="2026-09-27"`, `billing_margin=1.05`(FP-037: 첫 실행 뒤 콘솔 대조 전까지 여유 유지):
  - `anthropic.claude-sonnet-5.batch.input` / `.batch.output` / `.batch.cache-write-5m` / `.batch.cache-write-1h` / `.batch.cache-read`
  - `anthropic.claude-sonnet-5.input` / `.output` (일반 호출. 이번 phase는 쓰지 않지만 dry-run 비교 출력용)
- 테스트: 각 단가 값, 100만 토큰 × 단가 × 1.1(부가세) 계산, 기존 업스테이지 항목 불변.

### 3. 생성 설정 — `src/kodocs/llm/models.toml` + `src/kodocs/llm/settings.py`

```toml
[synth]
model = "claude-sonnet-5"
max_tokens = 6000
thinking = "disabled"        # "disabled" | "adaptive"
effort = ""                   # "" | "low" | "medium" | "high" (빈 문자열 = 보내지 않음)
temperature = ""              # 이 모델은 샘플링 파라미터 미지원 → 보내지 않고 null로 기록
max_attempts = 3
pilot_limit = 10
chars_per_token_estimate = 0.8   # dry-run 입력 토큰 추정 = ceil(문자 수 / 0.8). 보수적 가정, 파일럿 실측으로 보정
```
```python
@dataclass(frozen=True)
class GenerationConfig:
    model: str; max_tokens: int; thinking: Literal["disabled","adaptive"]; effort: str | None
    temperature: float | None; max_attempts: int; pilot_limit: int; chars_per_token_estimate: float
    def request_params(self) -> dict: ...     # model·max_tokens·thinking(·output_config.effort). temperature가 None이면 키 자체를 넣지 않는다
    def record(self) -> dict: ...             # 결과·manifest 기록용: {"model", "max_tokens", "thinking", "effort", "temperature": None, "sampling": "unsupported_by_model"}
    def settings_hash(self) -> str: ...
def load_generation_config(path: Path | None = None, section: str = "synth") -> GenerationConfig: ...
```
- 테스트: `request_params()`에 `temperature`·`top_p`·`top_k` 키가 없음, `thinking={"type": "disabled"}`, `record()["temperature"] is None`.

### 4. Batch 래퍼 — `src/kodocs/llm/anthropic_batch.py`

```python
BATCHES_MANIFEST = Path("data/manifest/anthropic_batches.jsonl")   # 커밋한다
SERVICE_PREFIX = "anthropic.claude-sonnet-5.batch"
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
@dataclass(frozen=True) class BatchRequest: custom_id: str; system: str; user: str; cache_key: str
@dataclass(frozen=True) class BatchResult:
    custom_id: str; result_type: Literal["succeeded","errored","canceled","expired","missing"]
    text: str | None; model: str | None; stop_reason: str | None; usage: Mapping[str, int] | None; error: str | None
class SubmitUncertainError(RuntimeError): ...   # 제출 성공 여부 불명 → 전체 중단
class SubmitRejectedError(RuntimeError): ...    # 서버가 거절(배치 미생성) → 전체 중단
class AnthropicBatchClient:
    def __init__(self, *, client_factory: Callable[[], object], config: GenerationConfig, ledger: CostLedger,
                 guard: BudgetGuard, manifest_path: Path = BATCHES_MANIFEST, clock=..., new_id=...): ...
    def estimate(self, requests: Sequence[BatchRequest]) -> CostEstimate: ...   # 예상 입력 토큰, 최대 출력 토큰(= n × max_tokens), 기대·최대 비용(부가세 포함), budget_estimate(여유 포함)
    def submit(self, requests: Sequence[BatchRequest]) -> str: ...                # batch_id
    def status(self, batch_id: str) -> BatchStatus: ...
    def collect(self, batch_id: str) -> list[BatchResult]: ...                    # 몇 번을 불러도 같은 결과, 원장 중복 기록 없음
    def mark_collected(self, batch_id: str) -> None: ...
    def open_batches(self) -> list[Mapping]: ...          # submitted 이후 collected 전
    def uncertain_submissions(self) -> list[Mapping]: ... # submitting·submit_uncertain 상태
    def adopt(self, submission_id: str, batch_id: str) -> None: ...     # 사용자가 콘솔에서 확인한 batch id를 연결
    def abandon(self, submission_id: str, reason: str) -> None: ...     # 사용자가 콘솔에서 배치가 없음을 확인
```

**manifest 상태 기록(append-only, 읽을 때 `latest_records(key=submission_id)`, FP-032)**

레코드 공통: `submission_id`(uuid hex), `state`, `ts`, `n_requests`, `custom_ids`, `request_set_hash`(요청 전체의 정규 JSON SHA-256), `generation`(`config.record()`), `settings_hash`. 상태 전이:
`submitting`(create 호출 **직전**에 기록) → `submitted`(+`batch_id`, `expires_at`) → `collected`(+`result_counts`, `usage_totals`: 결과 유형별 수와 succeeded usage 합) · 또는 `submit_rejected`(+`status_code`, 오류 요지) · `submit_uncertain`(+오류 종류) · `adopted`(+`batch_id`) · `abandoned`(+`reason`).

**제출(FP-035 이중 과금 방지)**

1. 모든 `custom_id`가 `CUSTOM_ID_RE`를 만족하고 배치 안에서 유일한지 검사. 위반이면 호출 없이 `ValueError`.
2. `uncertain_submissions()`가 하나라도 있으면 **호출하지 않고** `SubmitUncertainError`(메시지에 submission_id와 해결 방법: 콘솔에서 확인 후 `adopt` 또는 `abandon`).
3. `guard.check(budget_estimate)` — 최대 비용(입력 추정 + `n × max_tokens` 출력) × 부가세 × `billing_margin`. 초과면 `BudgetExceededError` 그대로.
4. `submitting` 기록 → **`max_retries=0`인 클라이언트**로 `batches.create` 1회. 이유: SDK 자동 재시도가 전송 후 실패를 재전송하면 배치가 두 번 만들어진다.
5. 결과 분류:
   - 성공 → `submitted` 기록, batch_id 반환
   - `APIStatusError`이고 `status_code < 500`(400·401·403·404·413·429 등) → 서버가 거절해 배치가 생성되지 않았다고 보고 `submit_rejected` 기록 후 `SubmitRejectedError`(재시도 안 함)
   - `status_code >= 500`, `APIConnectionError`(타임아웃 포함), 그 밖의 모든 예외 → 생성 여부 불명. `submit_uncertain` 기록 후 `SubmitUncertainError`. **절대 재제출하지 않는다.**
   - `submitting` 기록 직후 프로세스가 죽은 경우도 다음 실행에서 `uncertain_submissions()`로 잡힌다.

**조회·수거**

- `retrieve`·`results`·`list`는 읽기 전용이라 SDK 기본 재시도를 허용한다.
- `status()`: `processing_status`, 요청 수 집계, 생성 후 경과 시간. 생성 후 24시간이 지났는데 `ended`가 아니면 경고, **25일이 지났는데 수거하지 않았으면 경고**(결과 보관 29일).
- `collect()`: `processing_status != "ended"`면 `RuntimeError`. 결과를 `custom_id`로 매칭한다(순서 무시). 제출한 `custom_ids`에 없는 결과는 오류. 제출했는데 결과에 없는 `custom_id`는 `result_type="missing"`.
  - `succeeded`: 텍스트 블록을 이어 붙인 `text`, `model`, `stop_reason`, `usage`. **원장은 succeeded 결과의 usage로만 기록한다**: 입력 → `batch.input`, 출력 → `batch.output`, 캐시 쓰기·적중 토큰이 0보다 크면 해당 서비스. `ref=custom_id`, `run_id=batch_id`, `model=message.model`.
  - 원장 중복 방지: 같은 `(run_id, ref, service)` 기록이 원장에 이미 있으면 다시 기록하지 않는다(FP-033). 이유: 호출자가 결과 저장 도중 죽으면 `collect()`를 다시 부른다.
  - 원장 기록은 결과를 반환하기 **전에** 한다(M2와 같은 순서: 비용이 먼저 남아야 한다).
  - `errored`·`canceled`·`expired`: 원장 기록 없음(공식 문서상 미과금). `error`에 유형 문자열. 호출자가 재생성 대상으로 기록한다. `stop_reason == "max_tokens"`인 succeeded는 과금되므로 원장에 기록하고 그대로 반환한다(잘림 판정은 step 6).
  - 오류 객체의 속성은 문서 예시(`.result.error.type`)를 따르되, 형태가 다르면 `str()`로 남기고 실패로 멈추지 않는다(FP-011: 실제 응답으로 확인하기 전까지 추측한 속성에 의존하지 않는다).
- `mark_collected()`: `collected` 레코드(`result_counts`, `usage_totals`). 이 합계가 콘솔 사용량 대조(FP-037)의 기준이다.

**보안**

- API 키는 `client_factory` 안에서만 쓴다(래퍼는 키를 받지 않는다). 키·`Authorization`·`x-api-key`가 예외 메시지·manifest·원장·진행 출력에 나오지 않는다(가짜 키 문자열로 테스트).
- manifest·원장에 프롬프트·생성 본문을 넣지 않는다(해시·id·수치만).

### 5. 테스트 (필수)

- create 성공 → manifest `submitting`→`submitted`, 호출 1회.
- create에서 `APIConnectionError`/`APITimeoutError`/500 → `submit_uncertain`, `SubmitUncertainError`, **create 호출 정확히 1회**(재시도 0회). 다음 `submit()` → create 호출 0회로 `SubmitUncertainError`.
- create에서 400/401/429 → `submit_rejected`, `SubmitRejectedError`, 호출 1회.
- `submitting`만 있고 후속 기록이 없는 manifest → `submit()`가 호출 0회로 중단. `adopt()` 후에는 `open_batches()`에 나타나고, `abandon()` 후에는 새 제출이 가능.
- 클라이언트 생성 시 create용 `max_retries=0`이 전달되는지(`client_factory` 인자 검사).
- 결과를 역순·섞인 순서로 주는 가짜 → `custom_id` 매칭이 정확. 모르는 `custom_id` → 오류. 빠진 `custom_id` → `missing`.
- 4가지 결과 유형 혼합 → 원장 기록은 succeeded 건수 × (입력·출력) 줄만, errored·canceled·expired는 0줄.
- `collect()` 두 번 → 원장 줄 수 불변.
- 예산 초과 → create 호출 0회, `BudgetExceededError`.
- `custom_id` 65자·허용 외 문자·중복 → 호출 0회.
- 요청 params에 `temperature`·`top_p`·`top_k` 키 없음, `thinking`이 `{"type": "disabled"}`.
- 가짜 키 문자열이 어떤 출력·예외·manifest·원장에도 없음.
- `status()`의 24시간·25일 경고(가짜 clock).

## Acceptance Criteria

```bash
make verify
uv run python -c "import anthropic; print(anthropic.__version__)"
uv run python -c "from kodocs.costs import PRICES; print(sorted(k for k in PRICES if k.startswith('anthropic.')))"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 전송 후 실패에서 재제출 경로가 없는가(FP-035)? create가 `max_retries=0`인가?
   - 원장이 succeeded usage로만, 중복 없이 기록되는가(FP-033·FP-037)?
   - 결과를 순서가 아니라 `custom_id`로 매칭하는가?
   - `src/kodocs/llm/` 밖에서 `anthropic`을 import하지 않는가(AGENTS.md)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- Anthropic API를 실제로 호출하지 마라(`count_tokens` 포함). `.env`를 열거나 출력하지 마라. 이유: ADR-014, FP-040, AGENTS.md. 실제 실행은 사용자가 한다.
- 가격·만료·보관 기간을 네트워크로 다시 확인하거나 위 표와 다른 값을 쓰지 마라. 이유: 이 step의 "배경" 값이 공식 문서 확인 결과다(추측 금지 요구사항).
- `batches.create`를 재시도하거나, 성공 여부가 불명확한 제출 뒤에 새 제출을 하지 마라. 이유: FP-035(전송 후 실패 재시도 → 이중 과금).
- `temperature`·`top_p`·`top_k`를 요청에 넣지 마라. 이유: Sonnet 5는 400을 반환한다(AGENTS.md 샘플링 파라미터 규칙).
- errored·canceled·expired 결과를 원장에 기록하지 마라. 원장은 succeeded의 응답 usage 그대로 기록하고, 여유는 예산 검사에만 쓴다. 이유: 공식 문서상 미과금, FP-037(원장은 응답 값 그대로).
- 결과를 요청 순서(인덱스)로 짝짓지 마라. 이유: 공식 문서 "results can be returned in any order".
- SDK 내부(`anthropic._*`)나 문서에 없는 메서드를 쓰지 마라. 이유: FP-011(외부 라이브러리 동작 추측).
- 프롬프트 캐싱(`cache_control`)을 요청에 넣지 마라. 이유: ADR-024 — 절감이 작아 이번 phase는 쓰지 않는다. 단가 등록만 한다.
- `src/kodocs/parsing/`·`src/kodocs/synth/`를 수정하지 마라. 이유: 한 step 한 모듈.
- 기존 테스트를 깨뜨리지 마라.
