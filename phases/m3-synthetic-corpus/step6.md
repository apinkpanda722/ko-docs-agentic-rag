# Step 6: doc-validate

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 합성 문서 기관명 금지, 규정은 규정명·조항만, 정답은 구간 단위(fact ID·문자 범위)
- `docs/ADR.md` — ADR-002, ADR-010, ADR-016, ADR-019, **ADR-024**(검증 항목·인라인 표시·인젝션 삽입)
- `docs/EVAL.md` — 합성 qrels(시드 사실 ID·문자 범위), 보안(카나리아 누설 판정)
- step 0 산출물: `src/kodocs/synth/seed.py`(`surface_forms`, `find_banned_names`, `load_banned_names`, `PERIOD_*`), `src/kodocs/synth/identifiers.py`(`extract_identifiers`, `REGULATION_REF_RE`), `src/kodocs/synth/regulations.py`(`load_index`)
- step 3 산출물: `data/synthetic/plan.jsonl`, `canaries.json`, `injections.json`, `src/kodocs/synth/injection_texts.py`
- step 5 산출물: `src/kodocs/synth/generate.py`(`GENERATIONS_DIR`, `GENERATION_LOG`), 생성 캐시 파일 형식
- `src/kodocs/ingest/manifest.py` — `append_record`, `load_records`, `latest_records`

## 배경

생성 결과를 **LLM 없이** 검증한다. 통과한 문서만 최종 문서 JSON(`data/synthetic/docs/{doc_id}.json`)이 되고, 실패한 문서는 사유와 함께 기록되어 step 5가 다음 시도로 재생성한다. 최종 문서에는 표시를 벗긴 본문과 fact span(M4 qrels 정답 구간)을 기록한다. 인젝션 문구는 여기서 코드로 삽입한다.

## 작업

TDD: `tests/synth/test_validate.py`를 먼저 작성한다. 생성 캐시 fixture는 손으로 쓴 작은 Markdown(표시 포함)으로 만든다.

### 1. `src/kodocs/synth/validate.py`

```python
DOCS_DIR = Path("data/synthetic/docs")                           # 커밋
FAILURES_PATH = Path("data/synthetic/validation_failures.jsonl")  # 커밋, append-only
SUMMARY_PATH = Path("data/manifest/synth_validation.json")        # 커밋, 매 실행 덮어씀
MARKER_RE = re.compile(r"⟦F:(?P<fid>f-[a-z0-9]+(?:-[a-z0-9]+)*)⟧(?P<body>.*?)⟦/F⟧", re.S)
@dataclass(frozen=True) class Issue: code: str; detail: str; start: int | None = None; end: int | None = None
def strip_markers(text: str) -> tuple[str, list[tuple[str, int, int]], list[Issue]]: ...  # (본문, [(fact_id, start, end)] 벗긴 본문 기준, 표시 오류)
def insert_injection(body: str, text: str, rule: str) -> tuple[str, int, int]: ...     # (새 본문, 삽입 start, end)
def shift_spans(spans, at: int, length: int) -> list: ...
def validate_document(doc: DocPlan, generation: Mapping, *, seed: Seed, index, banned_names, canaries, injection) -> tuple[dict | None, list[Issue]]: ...
def check_canary_isolation(docs: Mapping[str, str], canaries: Sequence[Mapping]) -> dict[str, list[Issue]]: ...   # doc_id → 누설 이슈
def run(root: Path, *, print_fn=print) -> int: ...
def main(argv=None) -> int: ...   # python -m kodocs.synth.validate
```

**문서별 처리 순서(고정)**

1. 대상: 계획의 문서마다 **최신 시도**의 생성 캐시(`generation_log` + `generations/`, `latest_records`로 중복 제거, FP-032). succeeded가 아니면 건너뛰고 `not_generated`로 집계.
2. `stop_reason == "max_tokens"` → `truncated` 실패.
3. `strip_markers`: 표시를 벗긴 본문과 span 계산. 표시 오류 코드: `marker_malformed`(짝 없는 여는/닫는 표시, 중첩), `marker_unknown_fact`(계획에 없는 fact_id).
4. 인젝션 문서면 `injections.json`의 문구를 `insert_rule`(`before_last_h2`: 마지막 `\n## ` 줄 바로 앞에 `\n\n{text}\n\n`, 없으면 본문 끝)로 삽입하고 **삽입 지점 뒤의 span을 삽입 길이만큼 이동**한다. 인젝션 span을 기록한다.
5. 아래 검사를 최종 본문(표시 제거·인젝션 삽입 후)에서 한다. 모든 이슈를 모은다(첫 실패에서 멈추지 않음).

**검사 항목(코드 고정, 각 코드마다 fixture 테스트 필수)**

| 코드 | 판정 |
|---|---|
| `empty_body` | 제목 줄 제외 본문 200자 미만 |
| `fact_missing` | 계획의 `fact_ids`(카나리아 포함) 중 표시되지 않은 사실 |
| `fact_value_mismatch` | 원자 사실의 표시 구간 안에 `surface_forms` 중 어떤 것도 없음(공백·NFC 정규화 후 부분 문자열 비교). `text` 종류는 검사하지 않음 |
| `unassigned_identifier` | 본문 전체의 티켓·결재·장애 식별자(`extract_identifiers`) 중 이 문서에 배정된 `id` 사실 값이 아닌 것. 시드에 실제로 있는 ID여도 배정되지 않았으면 실패(정답 구간이 어긋나기 때문) |
| `unassigned_amount` | 본문의 금액 표현(`\d[\d,]*\s*원`, `\d+\s*만\s*원`, `\d+\s*억\s*원`)을 원 단위 정수로 바꿨을 때 배정된 `amount` 사실 값이 아닌 것 |
| `date_out_of_period` | 본문의 날짜 표현(`YYYY-MM-DD`, `YYYY.M.D`, `YYYY년 M월 D일`)이 2025-01-01~2026-06-30 밖이거나 존재하지 않는 날짜 |
| `banned_name` | `find_banned_names` 결과 1건 이상 |
| `regulation_unknown` | `REGULATION_REF_RE` 일치 항목의 (규정명, 조 키)가 규정 색인에 없음 |
| `regulation_unplanned` | 색인에는 있지만 계획의 `regulation_refs`에 없는 규정 조항 참조(경고가 아니라 실패. 이유: 틀린 인용 방지) |
| `canary_missing` | 제한 문서인데 자기 카나리아 값이 본문에 없음 |
| `canary_leak` | 문서 본문에 **다른 문서의** 카나리아 값이 있음(전 문서 교차 검사, `check_canary_isolation`) |
| `truncated` | 2번 |

- 경고(실패 아님, 요약에만): `length_off_target`(본문 길이가 계획의 `length_target_chars`의 50% 미만 또는 200% 초과).

**교차 검사와 기록**

- 모든 문서의 개별 검사가 끝난 뒤 `check_canary_isolation`을 통과 후보 전체 + 이번에 실패한 문서 본문에 대해 돌린다. 누설된 문서는 실패로 바꾼다.
- **카나리아는 SQLite에도 없어야 한다**: `db.build_rows(seed)`의 모든 문자열 값에 카나리아가 없음을 확인하고, 있으면 전체 실패(종료 코드 1).
- 통과 문서 → `docs/{doc_id}.json`(결정론 JSON, 임시 파일 → replace). 필드:
  `doc_id, doc_type, title(첫 줄 # 제거), team, access, author_id, date, body, fact_spans:[{fact_id,start,end}] (start 정렬), source_fact_ids, regulation_refs, canary:{canary_id,fact_id,value,start,end}|null, injection:{injection_id,variant,start,end}|null, related_doc_ids, generation:{model, generation(=config.record()), template_hash, prompt_sha256, doc_seed, attempt, cache_key, batch_id, custom_id, usage}, validated_with:{validator_version, seed_files_sha256, regulation_index_sha256}`
  - span 불변식(테스트 필수): 모든 span에 대해 `body[start:end]`가 원래 표시 안 텍스트와 같다(인젝션 삽입 뒤에도).
- 실패 문서 → `FAILURES_PATH`에 시도별 레코드 `{doc_id, attempt, cache_key, issues:[{code, detail}], ts}` 추가(append, 키 `(doc_id, attempt)`). 이미 같은 키 레코드가 있으면 추가하지 않는다. 이전에 통과해 `docs/`에 있던 문서가 이번에 실패하면 그 파일을 지우지 말고 요약에 `regressed`로 표시하고 종료 코드 1.
- 요약 `SUMMARY_PATH`: `{planned: 200, generated, passed, failed, not_generated, exhausted, by_code: {코드: 건수}, by_type: {...}, by_access: {...}, warnings: {...}, canary: {expected: 30, present, leaks}, injections: {expected: 12, inserted}}` (시각 없이 결정론).
- 문서 하나의 처리 예외(예: 깨진 캐시 JSON)는 `internal_error` 이슈로 그 문서만 실패 처리하고 계속한다(FP-030).
- **통과 0건이면 종료 코드 1**(FP-031). 생성 캐시가 하나도 없으면 "생성 결과 없음, `make generate` 먼저" 안내 후 종료 코드 1.
- 진행 출력: 20건마다 `flush=True`로 `[n/200] 통과 x 실패 y`(FP-034).

### 2. Makefile·AGENTS.md

- `synth-validate: uv run python -m kodocs.synth.validate` (`.PHONY` 추가)
- AGENTS.md 명령어 절: `make synth-validate # 생성 결과 결정론 검증 → data/synthetic/docs/·validation_failures.jsonl·data/manifest/synth_validation.json (네트워크 없음)`

### 3. 테스트 (필수)

- 표 모든 코드에 대해 위반 fixture 1개씩 → 해당 코드가 나옴. 위반 없는 fixture → 통과, 문서 JSON 생성.
- 날짜 허용 표기 5종이 모두 `fact_value_mismatch` 없이 통과. `125만 원`이 `1250000`으로 인식.
- `EXP-2026-0412`가 결재로만 인식되고 티켓으로 오인되지 않아 `unassigned_identifier`가 거짓으로 나지 않음.
- 인젝션 삽입 후 span 불변식 유지, 인젝션 span의 텍스트 = 레지스트리 문구.
- 카나리아가 다른 문서에 있으면 두 문서 중 누설된 쪽만 실패.
- 같은 입력 두 번 실행 → 문서 JSON·요약 바이트 동일, 실패 기록 줄 수 불변(FP-032).
- 깨진 캐시 JSON 1건 + 정상 2건 → 정상 2건 통과, 1건 `internal_error`, 종료 코드 0.
- 통과 0건 → 종료 코드 1.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.synth.validate; echo "exit=$?"   # 실제 생성 전이므로 "생성 결과 없음" 안내와 exit=1이 정상
grep -n "synth-validate" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 형식이 아니라 내용(배정 사실 존재·값 일치·날조·카나리아)으로 판정하는가(FP-001)?
   - 한 문서 실패가 전체를 멈추지 않는가(FP-030)? 통과 0건이 실패인가(FP-031)?
   - append-only 기록을 `latest_records`로 읽는가(FP-032)?
   - span 불변식 테스트가 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 검증에 LLM을 쓰지 마라. 이유: ADR 철학(결정론적으로 잴 수 있는 것은 LLM 없이), 비용.
- 실패 문서의 본문을 고쳐서(값 치환·표시 보정·기관명 삭제) 통과시키지 마라. 실패는 기록하고 재생성한다. 이유: 생성 결과를 조용히 바꾸면 span 정답과 생성 기록이 어긋난다(FP-020과 같은 원리).
- 경고 항목을 실패로, 실패 항목을 경고로 바꾸지 마라. 표의 판정을 그대로 구현한다. 이유: 재생성 비용과 정답 품질의 기준이다.
- 파일 존재·JSON 형식만 보고 통과 판정하지 마라. 이유: FP-001(형식만 보고 성공 판정 → 조용한 품질 실패).
- `data/synthetic/plan.jsonl`·레지스트리·시드·생성 캐시를 수정하지 마라. 이유: 검증 대상이자 기준이다.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
