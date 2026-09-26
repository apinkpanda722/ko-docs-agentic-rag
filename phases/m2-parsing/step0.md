# Step 0: cost-ledger

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL 규칙(실제 API 스크립트는 누적 비용 기록·월 상한 전 중단, `.env` 열람 금지)
- `docs/ARCHITECTURE.md` — 디렉토리 구조
- `docs/ADR.md` — ADR-014(실제 API 호출은 사용자가 실행), ADR-022(업스테이지 페이지당 과금)
- `docs/PRD.md` — 일정과 예산
- `src/kodocs/config.py` — `Settings.monthly_budget_usd`, `DEFAULT_MONTHLY_BUDGET_USD`
- `tests/test_config.py` — 기본값 테스트, `.env.example` 동기화 테스트
- `.env.example`
- `src/kodocs/ingest/manifest.py` — `append_record`, `load_records`, `now_iso` (재사용 가능 여부 확인)

## 배경

M2(업스테이지 파싱), M3(Anthropic 합성 생성), M6(OpenAI 답변)이 모두 같은 월 예산을 쓴다. 호출마다 비용을 append-only 원장에 남기고,
**달력 월(UTC) 합계 + 이번 호출 예상 비용이 상한을 넘으면 호출 전에 멈추는** 공용 모듈을 만든다. 이 step은 모듈만 만들고 어떤 API도 호출하지 않는다.

## 작업

TDD: `tests/test_costs.py`를 먼저 작성하고, 통과하는 구현을 작성한다.

### 1. `src/kodocs/costs.py` (새 파일, 최상위 모듈)

`retrieval`은 이 모듈을 import하지 않는다(필요 없다). 이 모듈은 `langchain`·`langgraph`를 import하지 않는다.

```python
TAX_RATE: float = 0.10   # 부가세 10% 포함 금액으로 기록한다 (보수적). 콘솔 사용량은 VAT 제외라 숫자가 다를 수 있다.

@dataclass(frozen=True)
class Price:
    service: str          # 예: "upstage.document-parse.standard"
    unit: str             # "page" | "input_token" | "output_token" ...
    unit_price_usd: float # 정가(세전)
    source_url: str
    checked_on: str       # "YYYY-MM-DD"

PRICES: Mapping[str, Price]   # 이번 step에서는 업스테이지 standard 하나만 등록:
# "upstage.document-parse.standard": unit="page", unit_price_usd=0.01,
#   source_url="https://console.upstage.ai/docs/models/compare/document-intelligence", checked_on="2026-09-26"

def estimate_usd(service: str, quantity: float, *, tax_rate: float = TAX_RATE) -> float: ...

@dataclass(frozen=True)
class LedgerEntry:
    call_id: str          # uuid4 hex. 호출 1회 = 1줄
    ts: str               # UTC ISO 8601 (끝에 "Z" 또는 "+00:00")
    service: str
    model: str            # 응답에 있는 실제 모델 버전(없으면 요청 모델명)
    unit: str
    quantity: float       # 실제 과금 수량 (업스테이지는 응답 usage.pages)
    unit_price_usd: float
    tax_rate: float
    amount_usd: float     # quantity * unit_price_usd * (1 + tax_rate)
    ref: str              # 입력 식별자(예: 입력 파일 sha256). 원문·URL·키를 넣지 마라.
    run_id: str

class CostLedger:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = ...): ...  # 기본 경로: data/manifest/costs.jsonl
    def record(self, *, service: str, model: str, quantity: float, ref: str, run_id: str) -> LedgerEntry: ...
    def month_total_usd(self, when: datetime | None = None) -> float: ...

class BudgetExceededError(RuntimeError): ...

class BudgetGuard:
    def __init__(self, ledger: CostLedger, monthly_budget_usd: float): ...
    def check(self, estimate_usd: float) -> None: ...   # month_total + estimate > budget 이면 BudgetExceededError
    def remaining_usd(self) -> float: ...
```

규칙:
- 원장은 **append-only JSONL**이다. 기존 줄을 고치거나 지우지 않는다. 한 줄은 청구 이벤트 하나다.
- **원장 합계는 모든 줄을 더한다. `latest_records`로 줄이지 마라.** 이유: FP-032의 규칙(append-only manifest는 키마다 마지막 줄)은 상태 기록용이다. 원장은 이벤트 기록이라 키로 줄이면 금액이 줄어 상한을 넘긴다.
- 월 합계 경계(FP-043: 무엇을 세는지): `ts`를 UTC로 해석해 **해당 월 1일 00:00:00 UTC 이상, 다음 달 1일 00:00:00 UTC 미만**인 줄만 더한다. 한국 시간 기준이 아니다. 경계값 테스트(월말 23:59:59Z, 월초 00:00:00Z, `+09:00` 표기 입력)를 둔다.
- 원장 파일이 없으면 합계 0. 깨진 줄(JSON 오류)이 있으면 **조용히 건너뛰지 말고** 줄 번호와 함께 예외를 낸다(비용을 적게 세는 쪽의 실패는 막는다).
- `check`는 `>`로 판정한다(합계+예상 == 상한이면 통과). `monthly_budget_usd=0`이면 모든 양수 예상 비용을 거부한다.
- 원장 레코드에 API 키, 인증 헤더, 요청 URL, 원문 텍스트를 넣지 않는다. 테스트로 확인한다.
- 시각은 주입 가능한 `clock`으로 받아 테스트에서 고정한다.

### 2. 설정 기본값 45

- `src/kodocs/config.py`의 `DEFAULT_MONTHLY_BUDGET_USD`를 `45.0`으로 바꾼다.
- `.env.example`의 `MONTHLY_BUDGET_USD=35`를 `MONTHLY_BUDGET_USD=45`로 바꾼다. 주석은 유지한다.
- `tests/test_config.py`에서 기본값 35를 가정한 부분을 45로 고친다. 동기화 테스트(`test_env_example_and_config_variable_names_match`)는 그대로 통과해야 한다.
- `.env.example`의 값과 `DEFAULT_MONTHLY_BUDGET_USD`가 같은지 확인하는 테스트를 추가한다(두 값이 다시 어긋나지 않게).

## Acceptance Criteria

```bash
make verify
uv run python -c "from kodocs.costs import PRICES, estimate_usd; print(round(estimate_usd('upstage.document-parse.standard', 1887), 2))"   # 20.76
grep -n "MONTHLY_BUDGET_USD=45" .env.example
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가? (`src/kodocs/costs.py` 추가는 step 8에서 문서에 반영한다)
   - ADR 기술 스택을 벗어나지 않았는가? (새 의존성 없음)
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `.env`를 열거나 출력하지 마라. 이유: AGENTS.md CRITICAL. 필요한 변수 이름은 `.env.example`에만 적는다.
- 실제 API를 호출하거나 네트워크 요청을 하지 마라. 이유: ADR-014, FP-040(지시에 없는 네트워크 요청 금지).
- `data/manifest/costs.jsonl` 실제 파일을 만들지 마라. 테스트는 `tmp_path`에 쓴다. 이유: FP-042(phase 실행 중 작업 트리 변경 금지), 원장은 사용자 실행으로만 생긴다.
- 원장 합계를 `latest_records`로 계산하지 마라. 이유: 위 규칙(이벤트 원장, FP-032 반대 방향).
- 크레딧·할인을 반영하지 마라. 이유: 원장은 정가 기준(보수적)이다.
- 업스테이지 외 가격(Anthropic·OpenAI·Gemini)을 추측으로 채우지 마라. 이유: 해당 phase(M3·M6)에서 공식 문서로 확인해 추가한다.
- 기존 테스트를 깨뜨리지 마라.
