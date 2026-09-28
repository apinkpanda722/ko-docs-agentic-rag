# Step 3: query-drafting

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 외부 LLM 호출은 `src/kodocs/llm/` 래퍼로만, 모델명·생성 설정 기록, 누적 비용 기록·월 상한, 실제 API 명령은 사용자가 실행, `.env`를 열지 않는다
- `docs/ADR.md` — ADR-003(보완), ADR-004, ADR-014, ADR-023(원장·billing_margin), ADR-024(Batch), **ADR-025**
- step 1: `src/kodocs/eval/golden.py`, step 2: `src/kodocs/eval/golden_specs.py`, `data/synthetic/queries/specs.jsonl`
- `src/kodocs/llm/anthropic_batch.py` — `AnthropicBatchClient`(`submit`, `open_batches`, `status`, `results`, `uncertain_submissions`, `adopt`, `abandon`), `anthropic_client_factory`
- `src/kodocs/llm/settings.py` — `GenerationConfig`, `load_generation_config(section=...)`, `models.toml`의 `[synth]` 섹션
- `src/kodocs/costs.py` — `CostLedger`, `BudgetGuard`, `estimate_usd`, `budget_estimate_usd`, `DEFAULT_LEDGER_PATH`
- `src/kodocs/synth/generate.py` — Batch 상태 기계 사용 예(제출 → 조회 → 수거, dry-run 출력, 진행 안내 문구), 이 파일은 **읽기만** 한다
- `src/kodocs/config.py` — 설정·월 상한 로더

## 배경

step 2 명세의 빈 `question` 칸을 Anthropic Batch(Sonnet 5)로 채운다. 모델에는 유형·주어 값·묻는 속성 이름·기준일·질문자 페르소나만 주고, **정답 구간 문장과 답 값은 주지 않는다**(어휘 누수·답 누수 방지). 수거한 초안은 결정론으로 검증하고, 실패한 문항만 재시도한다. 실제 API 호출은 사용자가 한다.

## 작업

TDD: `tests/eval/test_query_draft.py`를 먼저 작성한다. Batch 클라이언트는 가짜 객체로 mock한다. `make verify`는 API 키 없이 통과해야 한다.

### 1. 설정

- `src/kodocs/llm/models.toml`에 `[golden_queries]` 섹션: `model = "claude-sonnet-5"`, `max_tokens = 400`, `thinking = "disabled"`, `effort = ""`, `temperature = ""`(미지원 → 기록 `null`), `max_attempts = 3`, `chars_per_token_estimate = 0.8`. `load_generation_config(section="golden_queries")`로 읽는다.

### 2. `src/kodocs/eval/query_draft.py`

```python
DRAFTS_DIR = Path("data/synthetic/queries/drafts")            # 커밋: 수거 캐시(문항·시도별 JSON)
DRAFT_LOG = Path("data/synthetic/queries/draft_log.jsonl")    # 커밋, append-only
VALIDATION_PATH = Path("data/synthetic/queries/validated.jsonl")  # 커밋: 검증 통과 문항(질문 채움)
FAILURES_PATH = Path("data/synthetic/queries/draft_failures.jsonl")  # 커밋, append-only
BATCHES_MANIFEST = Path("data/manifest/anthropic_batches_golden.jsonl")   # 합성 생성 manifest와 분리
TEMPLATES_DIR = Path(__file__).parent / "templates" / "query_draft"      # system.md, user.md
def build_request(spec: Question, *, config, attempt: int) -> BatchRequest: ...   # custom_id = f"{spec.id}-a{attempt}"
def pending_specs(root, specs, *, config) -> list[tuple[Question, int]]: ...        # 통과 초안 없음 그리고 현재 템플릿 시도 < max_attempts
def validate_draft(spec: Question, text: str, *, seed, people) -> list[str]: ...   # 실패 코드 목록
def run(root, *, client, config, dry_run: bool, assume_yes: bool, limit: int | None, print_fn=print, input_fn=input) -> int: ...
def main(argv=None) -> int: ...   # python -m kodocs.eval.query_draft [--dry-run] [--limit N] [--yes] [--status]
```

- **manifest 분리(필수)**: `AnthropicBatchClient`를 `manifest_path=BATCHES_MANIFEST`로 만든다. `data/manifest/anthropic_batches.jsonl`(합성 생성 manifest)을 읽거나 쓰지 마라. 이유: 합성 생성 쪽에 수거 대기 배치가 열려 있어, 같은 manifest를 쓰면 두 상태 기계가 서로의 배치를 수거하려 든다.
- **프롬프트**: `templates/query_draft/system.md`(규칙: 한국어 사내 질문 한 문장, 주어 값은 그대로 쓸 것, 답을 추측해 넣지 말 것, 질문만 출력)와 `user.md`(유형, 주어 값, 묻는 속성 이름, 기준일, 질문자 팀·직급 페르소나). 답 값·정답 구간 문장·fact_id·person_id를 프롬프트에 넣지 마라(테스트로 강제: 모든 명세의 요청 본문에 `answer_values`의 어떤 값도, `p-NNN` 패턴도 없음). 템플릿 해시(system + user 바이트)를 시도 기록에 남긴다.
- **dry-run**: 네트워크·키 없이 대상 수, 유형별 수, 추정 입력 토큰, 예상 비용(Batch, VAT 포함)·최대 비용, 이번 달 누적, 상한, 남은 예산을 출력하고, **추정 입력 토큰과 예상 비용을 `data/manifest/golden_dry_runs.jsonl`에 append**한다(M3에서 배치 manifest에 추정값이 없어 결과 문서 빈칸이 생겼다). 최대 비용이 남은 예산을 넘으면 종료 코드 1.
- **실행**: 불명 제출이 있으면 새 제출 금지(안내만). 열린 배치가 있으면 조회·수거만 한다. 제출 전 `y` 확인(`--yes` 생략 가능). 수거 후 초안을 검증하고 통과 문항을 `VALIDATION_PATH`에 (id 정렬로) 다시 쓴다. 진행 중이면 실제 실행한 명령(`make queries`)을 다시 실행하라고 안내한다.
- **원장**: succeeded 결과의 usage로만 원장에 기록(ref=custom_id, run_id=batch_id). errored·canceled·expired는 원장에 없다. 예산 검사에는 `billing_margin`이 곱해진 추정을 쓴다(ADR-023, FP-037).
- **진행 출력**: 20건마다 `flush=True`(FP-034).

### 3. 초안 결정론 검증 `validate_draft` (각 코드 fixture 테스트 필수)

| 코드 | 판정 |
|---|---|
| `empty_or_multiline` | 빈 문자열, 2문장 이상, 또는 질문 외 설명 포함(여러 줄) |
| `anchor_missing` | `anchor_values` 중 하나라도 질문에 없음(NFC·공백 정규화 후 부분 문자열) |
| `answer_leak` | 원자 답: `answer_values`(허용 표기) 중 하나라도 질문에 있음. 서술 답: 주어 값을 뺀 질문과 답 사실 문장의 **내용 글자 바이그램 포함률**(질문 바이그램 중 답 문장에도 있는 비율, 공백·문장부호 제거)이 0.5 초과. identifier 유형은 조회 키(`anchor_values`의 ID) 자체는 주어이므로 이 검사에서 제외하고, 나머지 답 값은 똑같이 검사한다 |
| `as_of_missing` | temporal 문항인데 기준일(`as_of`의 허용 날짜 표기 중 하나)이 질문에 없음 |
| `prompt_artifact` | `p-NNN`, `fact`, `f-`로 시작하는 사실 ID, "명세", "주어 값" 같은 프롬프트 내부 표기 |

- `validated.jsonl`에는 명세의 모든 필드(`unmarked_candidates`, `no_answer_checks` 포함)를 그대로 두고 `question`만 채워 `golden.write_questions`로 쓴다. 필드가 보존됨을 테스트로 확인한다.
- 실패 문항은 `FAILURES_PATH`에 `{id, attempt, codes, ts}`로 append(키 `(id, attempt)`, 중복 추가 금지 — FP-032)하고 다음 실행에서 재시도 대상이 된다. 초안을 고쳐서 통과시키지 마라.
- step 4가 이 검증에 `overlap_cap` 코드와 태그 부여를 추가한다. `validate_draft`는 추가 검사를 붙이기 쉬운 구조(검사 함수 목록)로 둔다.

### 4. Makefile·AGENTS.md

- `queries-dry: uv run python -m kodocs.eval.query_draft --dry-run`
- `queries: uv run python -m kodocs.eval.query_draft`
- AGENTS.md 명령어 절:
  - `make queries-dry   # 합성 문항 질문 문장 Batch 예상 토큰·비용(추정값을 data/manifest/golden_dry_runs.jsonl에 기록) — 네트워크 없음`
  - `make queries       # 합성 문항 질문 문장 Batch 생성/수거 → 결정론 검증 — 실제 API 호출·과금, 사용자 실행`

## Acceptance Criteria

```bash
make verify
make queries-dry; echo "exit=$?"     # 네트워크·키 없이 대상 180건 추정이 출력되어야 한다
grep -n "queries-dry\|^queries:" Makefile && grep -n "make queries" AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make queries`(실제 호출)는 실행하지 마라.
2. 아키텍처 체크리스트를 확인한다:
   - LLM 호출이 `src/kodocs/llm/` 래퍼로만 가는가(AGENTS.md)? 모델명·생성 설정·템플릿 해시를 시도 기록에 남기는가?
   - 합성 생성 manifest(`anthropic_batches.jsonl`)를 건드리지 않는가?
   - 전송 후 실패를 재시도해 이중 과금할 경로가 없는가(FP-035)? 불명 제출 시 새 제출을 막는가?
   - 답 값·정답 문장이 프롬프트에 없음을 테스트가 강제하는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(dry-run 대상 수·예상 비용 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make queries`나 실제 API 호출을 실행하지 마라. `.env`를 열거나 출력하지 마라. 이유: ADR-014, AGENTS.md.
- 전송 후 실패(타임아웃·연결 끊김)를 재전송하지 마라. 불명 제출은 기록하고 사용자가 `--status`로 해결하게 하라. 이유: FP-035(이중 과금).
- 원장을 추정값이나 요청 수로 기록하지 마라. succeeded 결과의 usage만 기록한다. 이유: FP-037(원장은 공급자 청구와 대조할 실측이어야 한다).
- 합성 생성 manifest(`data/manifest/anthropic_batches.jsonl`)와 `src/kodocs/synth/`를 수정하지 마라. 이유: M3 후속(incident-007 수거)이 그 상태 기계를 쓴다.
- 검증에 실패한 초안을 코드로 고쳐 통과시키지 마라. 이유: FP-020(조용한 변경).
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
