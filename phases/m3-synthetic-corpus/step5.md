# Step 5: doc-generate

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — LLM 래퍼 규칙, 샘플링 파라미터 기록 규칙, 합성 문서 메타데이터(생성 모델·프롬프트 템플릿 해시·랜덤 시드·출처 fact ID), 실제 API 명령은 사용자 실행
- `docs/ADR.md` — ADR-002(규정은 규정명·조항만, 기관명 금지), ADR-003(보완: 시드는 프롬프트 변수 선택용), ADR-014, **ADR-024**(인라인 표시 `⟦F:fact_id⟧…⟦/F⟧`, 캐시 키, 파일럿)
- step 0 산출물: `src/kodocs/synth/seed.py`(`surface_forms`, `load_seed`), `src/kodocs/synth/identifiers.py`
- step 3 산출물: `src/kodocs/synth/plan.py`, `data/synthetic/plan.jsonl`, `data/synthetic/canaries.json`
- step 4 산출물: `src/kodocs/llm/anthropic_batch.py`(`AnthropicBatchClient`, `BatchRequest`, `BatchResult`, `SubmitUncertainError`, `SubmitRejectedError`), `src/kodocs/llm/settings.py`(`GenerationConfig`), `src/kodocs/llm/models.toml`, `src/kodocs/costs.py`
- `src/kodocs/parsing/run.py` — M2의 dry-run → 확인 입력 → 실행, 진행 출력 패턴(참고만, 수정 금지)

## 배경

계획(`plan.jsonl`)의 문서마다 프롬프트를 만들어 step 4 래퍼로 Batch 생성하고, 결과를 **커밋되는 생성 캐시**에 저장한다. 검증(step 6)은 이 캐시를 읽는다. Batch는 최대 24시간 걸리므로 `make generate`는 **다시 실행하면 이어서 진행하는 상태 머신**이다. 실제 API 호출은 사용자가 한다. 이 step의 테스트는 가짜 `AnthropicBatchClient`(또는 step 4의 가짜 SDK 클라이언트)로 한다.

## 작업

TDD: `tests/synth/test_generate.py`를 먼저 작성한다.

### 1. 프롬프트 템플릿 — `src/kodocs/synth/templates/`

- `system.md`: 회사 설정(가상 회사 `해온클라우드`, 8개 팀, 기간), 한국어 사내 문서 작성자 역할, **출력 규칙**:
  1. 결과는 Markdown 본문만. 첫 줄은 `# 제목`. 앞뒤 설명·코드 펜스 금지.
  2. 배정된 사실은 반드시 모두 쓰고, 쓸 때마다 `⟦F:fact_id⟧…⟦/F⟧`로 감싼다. 원자 사실(ID·날짜·금액·인물·서비스·건수·규정)은 감싼 구간 안에 제시된 값을 **제시된 표기 중 하나로** 쓴다. 서술 사실(text)은 요지를 자연스러운 문장으로 쓰고 그 문장을 감싼다. 같은 사실을 여러 번 쓰면 첫 번째만 감싸도 된다.
  3. 배정되지 않은 티켓·결재·장애 ID, 금액, 기간(2025-01-01~2026-06-30) 밖의 날짜를 만들지 않는다. 사람 이름은 제시된 인물만 쓴다.
  4. 실존 기관·기업·인물 이름을 쓰지 않는다. 규정은 제시된 "규정명 제N조"만 참조로 언급하고 **규정 내용을 단정해 옮기지 않는다**(예: "출장규칙 제8조에 따라 정산한다"는 가능, "제8조는 일비를 2만 원으로 정한다"는 금지).
  5. 카나리아(내부 코드명)가 제시되면 그 문자열을 정확히 한 번 이상 쓰고 표시로 감싼다.
- 유형별 템플릿 6개(`meeting.md`, `weekly.md`, `incident.md`, `runbook.md`, `guide.md`, `notice.md`): 섹션 구성 후보, 길이 목표, 문체 규칙. 자리표시자(`{title_hint}`, `{author}`, `{date}`, `{team}`, `{facts_block}`, `{regulations_block}`, `{canary_block}`, `{style_block}`)만 쓰고 `str.format_map`으로 채운다.
- 템플릿 해시: `template_hash(doc_type) = sha256(system.md 바이트 + b"\0" + {type}.md 바이트)`.
- `style` 변수(계획의 `tone`·`structure`·`persona`·`frame`)는 문체·구성 지시문으로 변환한다. `frame == "회고"`인 문서(회의록 최대 5건)는 "오래전 장애를 되돌아보는 회고 회의"라는 맥락을 지시한다. 계획의 `seed`는 이미 변수 선택에 쓰였다. 여기서 추가 난수를 쓰지 않는다.
- `facts_block`: 사실마다 `- fact_id | 종류 | 값 | 허용 표기: a / b / c | 문서 날짜 기준 N일 전` 한 줄(`surface_forms` 사용, 경과 일수는 계획의 `fact_age_days`, 날짜 없는 사실은 경과 일수 생략). 인물 사실은 이름·직급·팀. system 규칙에 "경과 일수에 맞게 시점을 표현한다(예: 0~7일 '이번 주', 8~30일 '지난달'·'최근', 90일 초과 '지난해'·'과거 사례로')"를 넣는다. 이유: 문서 날짜보다 오래된 사실을 방금 일어난 일처럼 쓰지 않게 한다.
- 계획 JSON의 키는 step 3 산출물에 `fact_age_days`(fact_id → 경과 일수)와 `style.frame`(`"일반"`|`"회고"`)이 추가된 형식이다. 테스트 fixture도 이 형식을 따른다.

### 2. `src/kodocs/synth/generate.py`

```python
GENERATIONS_DIR = Path("data/synthetic/generations")      # 커밋한다. {cache_key}.json
GENERATION_LOG = Path("data/synthetic/generation_log.jsonl")  # 커밋한다. 시도 기록(append-only)
def build_request(doc: DocPlan, seed: Seed, *, config: GenerationConfig, attempt: int) -> tuple[BatchRequest, dict]: ...  # (요청, 캐시 메타)
def cache_key(*, template_hash: str, facts: Sequence[Fact], doc_seed: int, attempt: int, config: GenerationConfig) -> str: ...
def pending_docs(root: Path, plan: Sequence[DocPlan], *, config) -> list[tuple[DocPlan, int]]: ...   # (문서, 다음 시도 번호)
def run(root: Path, *, client: AnthropicBatchClient | None, config, dry_run: bool, limit: int | None,
        assume_yes: bool, input_fn=input, print_fn=print, only: Sequence[str] | None = None) -> int: ...  # 종료 코드
def main(argv=None) -> int: ...   # python -m kodocs.synth.generate
```

**캐시 키(FP-033: 멱등성 키 = 출력을 결정하는 키)**

`cache_key = sha256(정규 JSON{"template_hash", "facts": [[fact_id, sha256(정규 JSON(fact 전체 필드))] 정렬], "doc_seed", "attempt", "model", "settings_hash", "prompt_sha256"})[:32]`
- fact 값이 바뀌면 키가 바뀐다(fact_id만으로 만들지 않는다).
- `prompt_sha256` = system + user 프롬프트 전문 해시(템플릿 밖 입력 변화도 반영).
- `custom_id = f"{doc_id}-a{attempt}"`(`^[a-zA-Z0-9_-]{1,64}$` 검사).
- 같은 `cache_key`의 `generations/{cache_key}.json`이 있으면 **절대 다시 요청하지 않는다**.

**생성 캐시 파일** `generations/{cache_key}.json`(결정론 JSON): `{"doc_id", "attempt", "custom_id", "cache_key", "batch_id", "result_type", "text"(표시 포함 원문, succeeded만), "model", "stop_reason", "usage", "generation": config.record(), "template_hash", "prompt_sha256", "doc_seed", "fact_ids", "collected_at"}`. errored·canceled·expired·missing도 파일로 남긴다(`text=null`). 이유: 시도 횟수를 세고 재생성 대상을 정하기 위해.

**시도·재생성**

- 문서의 다음 시도 번호 = 그 문서의 기존 시도 수 + 1. 기존 시도는 `generations/`의 파일 + `generation_log.jsonl`(`latest_records(key=(doc_id, attempt))`, FP-032)로 센다.
- 문서가 **재생성 대상**인 경우: 마지막 시도가 succeeded가 아님(errored·canceled·expired·missing), 또는 step 6 검증 실패 기록(`data/synthetic/validation_failures.jsonl`의 최신 기록이 그 시도를 실패로 표시)이 있음.
- 시도는 문서당 최대 `config.max_attempts`(3)회. 초과하면 요청하지 않고 `exhausted`로 요약에 표시한다.
- 검증을 통과한 문서(`data/synthetic/docs/{doc_id}.json`이 있고 그 `cache_key`가 최신 시도와 같음)는 대상이 아니다.

**상태 머신(`run`)** — 매 실행 시 이 순서:

1. `client.uncertain_submissions()`가 있으면 해결 방법(콘솔 확인 후 `--adopt-batch SUBMISSION_ID BATCH_ID` 또는 `--abandon-submission SUBMISSION_ID`)을 출력하고 종료 코드 2. 새 제출을 하지 않는다(FP-035).
2. `client.open_batches()`가 있으면 각각 `status()`를 출력한다. `ended`면 `collect()` → 결과마다 생성 캐시 파일 쓰기(임시 파일 → replace) → `generation_log`에 기록 → 모두 저장한 뒤 `mark_collected()`. 끝나지 않은 배치가 있으면 "진행 중, 나중에 `make generate`를 다시 실행" 안내 후 종료 코드 0. **열린 배치가 있으면 새 제출을 하지 않는다.**
3. 대상 문서 목록 계산. `--limit N`(파일럿)은 계획 순서의 앞 N개가 아니라 **유형·권한별로 고르게** 뽑은 N개(결정론)로 제한한다.
4. 대상 0건: 계획된 200건 중 생성 캐시가 하나도 없으면 "생성 0건" 실패로 종료 코드 1(FP-031). 모두 처리됐으면 요약 출력 후 0.
5. 계획 출력: 대상 문서 수(유형·권한별), 추정 입력 토큰, 최대 출력 토큰, 예상 비용(기대·최대, 부가세 포함, Batch 단가), 같은 요청을 일반 호출로 했을 때 비용(참고), 이번 달 누적, 상한, 실행 후 남은 예산. 최대 비용(여유 포함)이 남은 예산을 넘으면 그 사실을 출력하고 종료 코드 1.
6. `--dry-run`이면 여기서 종료(네트워크 없음, 키 불필요).
7. `계속하려면 y 입력:` 확인(`--yes`면 생략). `y`가 아니면 호출 없이 종료.
8. `submit()` → batch_id 출력. `generation_log`에 시도별 `submitted` 기록. 안내: "결과는 최대 24시간 뒤. `make generate`를 다시 실행하면 수거한다".
- `SubmitUncertainError`·`SubmitRejectedError`·`BudgetExceededError`는 메시지를 출력하고 종료 코드 2(키 비노출).
- 진행 출력(FP-034): 요청 생성·결과 저장 중 20건마다 `flush=True`로 `[n/total]` 줄. 진행 줄에 키·프롬프트를 넣지 않는다.
- 결과 하나의 저장 실패(예: 깨진 텍스트)는 그 결과만 실패 기록하고 계속한다(FP-030). 저장 실패가 하나라도 있으면 `mark_collected()`를 하지 않는다(다음 실행에서 다시 수거, 원장 중복은 step 4가 막음).

**CLI**

- `--dry-run`, `--limit N`, `--yes`, `--only DOC_ID ...`, `--status`(상태만 출력), `--adopt-batch SUBMISSION_ID BATCH_ID`, `--abandon-submission SUBMISSION_ID --reason TEXT`
- 키는 dry-run이 아닐 때만 `load_settings().require_key("ANTHROPIC_API_KEY")`로 읽고, 없으면 안내 후 종료 코드 2.
- 실제 클라이언트 팩토리: `anthropic.Anthropic(api_key=..., max_retries=...)` 생성은 `src/kodocs/llm/` 안의 함수로 둔다(`synth`에서 `anthropic`을 직접 import하지 않는다).

### 3. Makefile·AGENTS.md

- `generate-dry: uv run python -m kodocs.synth.generate --dry-run`
- `generate-pilot: uv run python -m kodocs.synth.generate --limit 10`
- `generate: uv run python -m kodocs.synth.generate` (step 8이 검증·지표 단계를 뒤에 붙인다)
- `.PHONY` 추가. AGENTS.md 명령어 절:
  - `make generate-dry   # 합성 문서 생성 예상 토큰·비용(Batch)만 출력 — 네트워크 없음`
  - `make generate-pilot # 파일럿 10건 Batch 제출/수거 — 실제 API 호출·과금, 확인 후 실행, 사용자 실행`
  - `make generate       # 합성 문서 Batch 생성(재실행 시 상태 조회·수거·재생성 이어서) — 실제 API 호출·과금, 사용자 실행`
- 기존 `make generate` 설명 줄이 있으면 위 내용으로 바꾼다.

### 4. 테스트 (필수, 가짜 클라이언트)

- 같은 입력 → 같은 `cache_key`. fact 값 하나만 바꿔도 키가 바뀜. 템플릿 바이트가 바뀌면 `template_hash`와 키가 바뀜.
- 생성 캐시가 있는 문서 → `submit` 호출 0회.
- 처음 실행: dry-run → `submit` 0회, 출력에 예상 비용. `n` 입력 → 0회. `y` → 1회, `custom_id` 형식 `syn-…-a1`.
- 열린 배치(in_progress) → 새 제출 0회, 종료 코드 0. `ended` → 수거·캐시 파일 생성·`mark_collected` 1회.
- 불명 제출 있음 → `submit` 0회, 종료 코드 2, 안내 문구.
- 결과 유형 혼합(succeeded 2, errored 1, expired 1) → 다음 실행에서 errored·expired 문서만 `a2`로 재요청. 3회 소진 문서는 요청 0회, `exhausted` 표시.
- 검증 실패 기록이 있는 문서 → 다음 시도로 재요청.
- `--limit 10` → 10건, 유형이 2개 이상 섞임.
- 결과 저장 1건 실패 → 나머지 저장, `mark_collected` 미호출.
- 생성 캐시 0건 + 대상 0건(예: 계획 비어 있음) → 종료 코드 1.
- 요청 params에 `temperature` 없음, 생성 캐시의 `generation.temperature is None`.
- 프롬프트에 계획의 모든 fact_id와 카나리아(제한 문서만)가 들어가고, 제한 문서가 아닌 문서의 프롬프트에는 어떤 카나리아 값도 없음.
- 진행 줄이 flush되어 출력됨.

## Acceptance Criteria

```bash
make verify
make generate-dry     # 네트워크 없이 200건 계획의 예상 토큰·비용 출력, 종료 코드 0
grep -n "generate" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다(`make generate-dry`만. 파일럿·전체 생성은 실행하지 않는다).
2. 아키텍처 체크리스트를 확인한다:
   - 캐시 키에 fact 값 해시·템플릿 해시·시드·시도·모델·설정이 모두 들어가는가(FP-033)?
   - 열린 배치·불명 제출이 있을 때 새 제출이 불가능한가(FP-035)?
   - 생성 0건이 성공으로 끝나지 않는가(FP-031)? 진행 줄이 flush되는가(FP-034)? 한 건 실패 후 계속하는가(FP-030)?
   - `synth`가 `anthropic`을 직접 import하지 않는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make generate`·`make generate-pilot`을 실행하거나 Anthropic API를 호출하지 마라. `.env`를 열지 마라. 이유: ADR-014, FP-040. 실제 호출·과금은 사용자가 한다.
- 같은 `cache_key`로 다시 요청하는 경로를 만들지 마라(`--force` 포함). 재생성은 시도 번호를 올려 새 키로만 한다. 이유: FP-033(재실행 재과금).
- 열린 배치나 불명 제출이 있는데 새 배치를 제출하지 마라. 이유: FP-035(이중 과금), 같은 문서가 두 배치에 들어간다.
- 프롬프트 템플릿에 실존 기관명·규정 조문 내용을 넣지 마라. 이유: AGENTS.md CRITICAL, ADR-002.
- 결과를 순서로 짝짓지 마라. `custom_id`로만 짝짓는다. 이유: 공식 문서상 결과 순서 무보장.
- 생성 본문의 표시를 이 step에서 제거·수정하지 마라. 원문 그대로 캐시에 저장한다. 이유: span 계산과 검증은 step 6이 한다.
- `src/kodocs/llm/anthropic_batch.py`의 제출·원장 규칙을 우회하거나 수정하지 마라(클라이언트 팩토리 함수 추가만 허용). 이유: 한 step 한 모듈, FP-035·FP-037 규칙 보존.
- 기존 테스트를 깨뜨리지 마라.
