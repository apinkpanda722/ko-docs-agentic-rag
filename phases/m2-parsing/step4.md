# Step 4: upstage-adapter

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL(실제 API 스크립트는 비용 기록·상한 전 중단, `.env` 금지), 명령어 절
- `docs/ADR.md` — ADR-014, ADR-022(업스테이지 페이지당 과금)
- `src/kodocs/config.py` — `load_settings()`, `require_key("UPSTAGE_API_KEY")`
- `src/kodocs/ingest/http.py` — httpx 사용 방식, 주입 가능한 `transport`·`sleep`·`clock` 패턴(참고만. 이 파일은 수정하지 않는다)
- step 0 산출물: `src/kodocs/costs.py` (`CostLedger`, `BudgetGuard`, `estimate_usd`, `BudgetExceededError`)
- step 1 산출물: `src/kodocs/parsing/schema.py`, `src/kodocs/parsing/tables.py`(`place_html_rows`, `cells_to_grid`)
- step 2 산출물: `src/kodocs/parsing/run.py`, `tests/parsing/test_run.py`

## 배경 — 공식 문서로 확인한 사양 (2026-09-26, console.upstage.ai/docs)

- 엔드포인트: `POST https://api.upstage.ai/v1/document-digitization`, `Authorization: Bearer <UPSTAGE_API_KEY>`, `multipart/form-data`
- 폼 필드: `document`(파일), `model`(`document-parse`), `mode`(`standard`|`enhanced`|`auto`, 기본 standard), `ocr`(`auto`|`force`, 기본 auto),
  `output_formats`(JSON 배열 문자열, 예: `'["text","html","markdown"]'`, 기본 `["html"]`), `coordinates`(기본 true), `chart_recognition`(기본 true),
  `merge_multipage_tables`(기본 false), `base64_encoding`(기본 없음)
- 응답: `apiVersion`, `model`(실제 버전, 예: `document-parse-260630`), `elements[]`(`id`, `category`, `page`, `content.{text,html,markdown}`), `content`, `usage.pages`(과금 쪽수), `usage.standard`, `usage.enhanced`
- 동기 API는 **파일당 100쪽까지**(초과분은 앞 100쪽만 처리). 동기 파일 크기는 FAQ 기준 50MB로 보수적으로 잡는다.
- 가격: standard 쪽당 $0.01, enhanced $0.03(`auto`는 표가 많은 쪽을 enhanced로 보내 단가가 오를 수 있다 → `mode=standard` 고정)
- 속도 제한 Tier 0 동기: 1 RPS, 300 PPM. 429 응답의 `X-Upstage-RateLimit-Retry-After-Requests` 헤더는 **재시도 가능 시각(Unix 초)**이다(대기 초가 아니다). 공식 권장은 순차 호출 + 지수 백오프.
- 사용자가 업스테이지 콘솔에 월 한도 $25를 따로 걸어 두었다(서비스 쪽 차단). 코드의 원장·상한은 이와 별개로 동작한다.

## 작업

TDD: `tests/parsing/test_upstage_adapter.py`를 먼저 작성한다. HTTP는 `httpx.MockTransport`로, 시간은 가짜 `clock`/`sleep`으로, 확인 입력은 가짜 `input`으로 주입한다.

### 1. `src/kodocs/parsing/upstage_adapter.py`

```python
PARSER_NAME = "upstage"
SERVICE = "upstage.document-parse.standard"   # costs.PRICES 키
REQUEST_OPTIONS: Mapping[str, str] = {        # 고정. 바꾸면 settings_hash와 캐시 검사가 바뀐다
    "model": "document-parse",
    "mode": "standard",
    "ocr": "auto",
    "output_formats": '["text", "html", "markdown"]',   # 출력 형식은 비용과 무관. 한 번 받아 캐시하면 재과금 없이 모든 형식을 쓸 수 있다
    "coordinates": "true",
    "chart_recognition": "true",
    "merge_multipage_tables": "false",
}
MAX_PAGES_PER_REQUEST = 100
MAX_FILE_BYTES = 50 * 1024 * 1024

def cache_path(root: Path, input_kind: str, input_sha256: str) -> Path: ...
    # data/raw/upstage/{input_kind}/{input_sha256}.json  (step 9가 hwp5/hwpx로 재사용)

class UpstageClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None,
                 min_interval_s: float = 1.0, max_retries: int = 5, backoff_s: float = 2.0, timeout_s: float = 300.0,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep): ...
    def parse(self, path: Path) -> tuple[dict, float]: ...   # (응답 JSON, 소요 초)

class UpstageAdapter:
    name = PARSER_NAME
    def __init__(self, root: Path, *, client_factory: Callable[[], UpstageClient], ledger: CostLedger, guard: BudgetGuard, run_id: str): ...
    def settings(self) -> Mapping[str, object]: ...   # {"input": "pdf", "request_options": REQUEST_OPTIONS}. 응답의 실제 model 버전은 문서마다 ParsedDocument.parser_version에 둔다
    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument: ...

def response_to_parsed(resp: Mapping, *, item: ParseInput, root: Path, settings: Mapping[str, object], elapsed_s: float) -> ParsedDocument: ...
def plan(root: Path, inputs: Sequence[ParseInput]) -> DryRunPlan: ...   # 캐시 없는 입력 수, 쪽수 합, 예상 비용(세금 포함), 이번 달 누적, 상한, 남은 예산
```

**캐시와 재과금 방지 (FP-033)**
- 캐시 키 = 출력을 결정하는 키 = `(input_kind, input_sha256)`. 캐시 파일에 `{"request_options": REQUEST_OPTIONS, "fetched_at", "elapsed_s", "response": {...}}`를 저장한다(임시 파일 → `replace`).
- `parse()`는 캐시가 있으면 **절대 호출하지 않고** 캐시로 `ParsedDocument`를 만든다. `--force` 재파싱도 캐시를 쓴다. 캐시를 지우는 기능은 만들지 않는다(사용자가 직접 지운다).
- 캐시의 `request_options`가 현재 `REQUEST_OPTIONS`와 다르면 호출하지 않고 `ParseError("캐시 옵션 불일치 …")`. 조용히 다시 호출해 재과금하지 않는다.
- 캐시에 API 키·인증 헤더를 넣지 않는다(테스트로 확인).

**호출 전 검사 (순서 고정)**
1. `page_count`가 없거나 `> MAX_PAGES_PER_REQUEST`, 또는 파일 크기 `> MAX_FILE_BYTES` → 호출하지 않고 `ParseError`(실행기가 `failed`로 기록 후 계속).
2. `guard.check(estimate_usd(SERVICE, page_count))` → 초과 시 `BudgetExceededError`를 그대로 올린다(실행기가 전체 중단).

**HTTP (순차, FP-011·FP-030)**
- 요청 사이 최소 간격 `min_interval_s`(1 RPS). 동시 호출하지 않는다.
- 429: `X-Upstage-RateLimit-Retry-After-Requests`(Unix 초)가 있으면 `max(0, 그 값 - clock())`만큼, 없으면 `backoff_s * 2**attempt`만큼 기다린 뒤 재시도. 최대 `max_retries`회 후 `ParseError`.
- 5xx·연결 오류·타임아웃: 같은 백오프로 재시도. 그 밖의 4xx: 재시도 없이 `ParseError`(상태 코드와 응답 본문 앞 200자, 키 제외).
- 200인데 JSON이 아니거나 `usage.pages`가 없으면 `ParseError`. 이 경우 원장에 기록하지 않지만 응답 본문 일부를 오류에 남겨 사용자가 콘솔 사용량과 대조할 수 있게 한다.
- 오류 메시지·로그·진행 줄에 API 키나 `Authorization` 헤더를 넣지 않는다. 테스트: 가짜 키 문자열이 어떤 출력·예외·캐시에도 나오지 않는다.

**원장 기록**
- 200 + `usage.pages`가 있으면 **캐시를 쓰기 전에** `ledger.record(service=SERVICE, model=resp["model"], quantity=usage.pages, ref=input_sha256, run_id=...)`. (캐시 쓰기가 실패해도 비용은 남아야 한다.)
- `usage.enhanced`가 비어 있지 않으면 경고를 출력하고 settings와 실행 기록에 남긴다(standard 고정이 깨졌다는 신호).

**응답 → 공통 스키마**
- `elements`를 `id` 순으로 `Block(page, kind, content.text)`로 옮긴다. category 매핑: `heading1`·`heading`·`title` 계열 → `heading`, `paragraph` → `text`, `list` → `list_item`, `header` → `header`, `footer` → `footer`, `caption` → `caption`, `table` → `table`, 나머지 → `other`.
- `category == "table"`인 요소는 `content.html`의 `<table>`을 표준 라이브러리 `html.parser`로 읽어 행마다 `(text, rowspan, colspan)` 목록을 만들고 **`tables.place_html_rows` → `TableCell`**로 바꾼다. 격자화는 `cells_to_grid`만 쓴다. `<br>`은 공백 하나.
- `parser_version`은 응답 `model`(예: `document-parse-260630`), settings에 `apiVersion`도 남긴다. `elapsed_s`는 캐시에 저장된 최초 호출 시간.

### 2. CLI 연결 — `run.py` 확장

- `--parser upstage`를 `ADAPTERS`에 등록한다. 키는 `load_settings().require_key("UPSTAGE_API_KEY")`로 **실행 시점에만** 읽는다(없으면 안내 후 종료 코드 2).
- `--dry-run`: `plan()` 결과만 출력하고 끝낸다(네트워크 없음, 키 불필요). 출력 예:
  `캐시 없음 12건 / 186쪽 / 예상 $2.05 (세금 10% 포함) / 이번 달 누적 $0.00 / 상한 $45.00 / 실행 후 남은 예산 $42.95`
  예상 비용이 남은 예산을 넘으면 그 사실을 함께 출력하고 종료 코드 1.
- dry-run이 아닌 실행은 같은 계획을 출력한 뒤 `계속하려면 y 입력:`으로 확인을 받는다. `y` 외 입력이면 호출 없이 종료. `--yes`면 확인을 생략한다. 입력 함수는 주입 가능하게 한다.
- 캐시가 있는 입력은 계획의 호출 대상에서 뺀다.

### 3. Make 타깃

- `parse-upstage-dry: uv run python -m kodocs.parsing.run --parser upstage --dry-run`
- `parse-upstage: uv run python -m kodocs.parsing.run --parser upstage`
- `.PHONY`에 추가. `AGENTS.md` 명령어 절:
  `make parse-upstage-dry  # 업스테이지 예상 비용(캐시 없는 대상의 쪽수 × 단가) 출력만 — 네트워크 없음`,
  `make parse-upstage      # 업스테이지 Document Parse(standard) 파싱 — 실제 API 호출·과금, 확인 후 실행, 사용자 실행`

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.parsing.run --help | grep -n -- "--dry-run"
grep -n "parse-upstage" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 테스트: 캐시 있음 → 호출 0회, 캐시 옵션 불일치 → 호출 0회 + 오류, 429(헤더 있음/없음) 대기·재시도, 4xx 비재시도, 100쪽 초과 비호출, 상한 초과 → `BudgetExceededError`로 전체 중단, 원장 기록이 캐시 쓰기보다 먼저, 키 문자열 비노출, dry-run 네트워크 0회, 확인 입력 `n` → 호출 0회.
   - `mode=standard`가 요청 폼에 실제로 들어가는가(MockTransport에서 폼 필드 검사)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 업스테이지 API를 실제로 호출하지 마라. `make parse-upstage`, `make parse-upstage-dry`를 실데이터로 실행하지 마라. 이유: ADR-014, FP-040. 과금·실행은 사용자가 한다.
- `.env`를 열거나 출력하지 마라. 키를 로그·예외·캐시·원장·진행 줄에 남기지 마라. 이유: AGENTS.md CRITICAL, ADR-014.
- `mode`를 `auto`·`enhanced`로 바꾸거나 옵션으로 열지 마라. 이유: 단가가 3배가 될 수 있고 비교 조건이 바뀐다.
- 캐시가 있는데 다시 호출하는 경로를 만들지 마라(옵션 불일치 포함). 이유: FP-033, 재실행 재과금.
- 병렬·동시 요청을 하지 마라. 이유: Tier 0 1 RPS, 공식 권장은 순차 호출.
- 100쪽 넘는 파일을 쪼개 보내는 기능을 만들지 마라. 이유: 대상 최대 85쪽이라 불필요, 분할은 쪽 경계에서 조문을 끊어 비교 조건을 바꾼다.
- `src/kodocs/ingest/http.py`를 수정하지 마라. 이유: 한 step 한 모듈. 필요한 HTTP 로직은 어댑터 안에 둔다.
- 기존 테스트를 깨뜨리지 마라.
