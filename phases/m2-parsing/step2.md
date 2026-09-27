# Step 2: parse-runner

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 명령어 절
- `docs/ARCHITECTURE.md`, `docs/ADR.md` — ADR-008, ADR-014
- `data/manifest/parse_targets.json` — `targets[]` 필드
- `src/kodocs/ingest/convert.py` — 배치 실행, 진행 출력(`flush=True`), 실패 후 계속, manifest 기록 패턴
- `src/kodocs/ingest/manifest.py` — `append_record`, `latest_records`, `repo_relative`, `sha256_file`
- `tests/ingest/test_convert.py` — 진행 출력·실패 계속 테스트 방식
- step 1 산출물: `src/kodocs/parsing/schema.py`, `src/kodocs/parsing/tables.py`, `src/kodocs/parsing/pypdf_adapter.py`

## 배경

파서마다 같은 대상 목록(`parse_targets.json`, 115개: 현행 95 + 과거 20)을 같은 방식으로 돌리는 실행기를 만든다.
어댑터는 등록만 하면 되고, 멱등성·실패 기록·진행 출력은 실행기 한 곳에서 처리한다. 이 step에서는 pypdf만 등록한다.

## 작업

TDD: `tests/parsing/test_run.py`를 먼저 작성한다. 가짜 어댑터(성공/예외/빈 텍스트)와 `tmp_path`의 가짜 `parse_targets.json`을 쓴다.

### 1. `src/kodocs/parsing/run.py`

```python
@dataclass(frozen=True)
class ParseInput:
    target_key: str        # f"{law_rule_id}:{law_serial}:{source_sha256}" — 대상 식별
    input_path: Path
    input_sha256: str      # 파일을 실제로 읽어 계산 (manifest 값과 다르면 실패로 기록)
    input_kind: str        # "pdf" (step 9에서 hwp5/hwpx 추가)
    page_count: int | None

class Adapter(Protocol):
    name: str
    def settings(self) -> Mapping[str, object]: ...
    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument: ...

ADAPTERS: dict[str, Callable[[], Adapter]]   # 이 step: {"pypdf": ...}

def load_inputs(root: Path, *, limit: int | None = None) -> list[ParseInput]: ...   # parse_targets.json의 pdf_path 기준
def run(root: Path, adapter: Adapter, inputs: Sequence[ParseInput], *, force: bool = False,
        progress_every: int = 10, out: TextIO = sys.stdout) -> RunSummary: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.parsing.run --parser pypdf [--limit N] [--force]
```

규칙:
- **출력**: `schema.output_path(root, parser, input_sha256)` = `data/interim/parsed/{parser}/{input_sha256}.json`.
- **멱등성(FP-033)**: 건너뛰기 판단 키는 출력을 결정하는 키와 같게 한다 = `(parser, input_sha256)`. 출력 파일이 있고 그 안의 `settings_hash`가 현재 어댑터 설정 해시와 같으면 건너뛴다(`skipped`). 설정이 다르면 다시 파싱한다. `--force`면 항상 다시 파싱한다. 같은 `input_sha256`이 대상 목록에 두 번 나오면 한 번만 파싱한다.
- **실행 기록**: `data/manifest/parse_runs.jsonl`(커밋 대상, 원문 없음)에 입력 1건마다 한 줄 append:
  `run_id`, `parser`, `parser_version`, `settings_hash`, `input_sha256`, `input_kind`, `target_key`, `status`(`ok`|`skipped`|`failed`|`empty`), `elapsed_s`, `page_count`, `text_chars`, `hangul_distinct`, `blocks`, `tables`, `invalid_tables`, `error_type`, `error`(메시지 첫 줄, 200자 이내), `ts`.
  읽을 때는 `latest_records(key=(parser, input_sha256))`로 읽는다(FP-032).
- **내용 지표 판정(FP-001)**: 어댑터가 예외 없이 끝나도 `hangul_distinct == 0`이면 `status="empty"`로 기록한다(출력 파일은 남긴다). 형식만 보고 성공 판정하지 않는다.
- **실패 후 계속(FP-030)**: 어댑터 예외(`ParseError` 포함 모든 `Exception`)는 그 입력만 `failed`로 기록하고 다음 입력으로 간다. 대상 목록을 읽지 못하는 경우(파일 없음·JSON 오류)만 전체 중단.
  예외: `costs.BudgetExceededError`는 잡지 말고 전체를 멈춘다(상한 초과 후 계속 호출하면 안 된다). 이 규칙을 테스트로 고정한다.
- **입력 검증**: `pdf_path`가 없거나 파일이 없으면 그 입력을 `failed`(`error_type="missing_input"`)로 기록하고 계속. 대상 목록이 0건이면 실패로 멈추고 `make parse-targets`를 먼저 실행하라고 안내한다(FP-031).
- **진행 출력(FP-034)**: `progress_every`건마다와 마지막에 `[pypdf] 20/115 ok=18 skipped=0 failed=1 empty=1 elapsed=12.3s` 형식의 줄을 `print(..., flush=True)`로 출력한다. 진행 줄에 경로 외 원문·URL·키를 넣지 않는다.
- **요약**: 끝나면 상태별 건수, 총 쪽수, 총 시간, 쪽당 시간(초/쪽)을 출력한다. `failed`가 1건 이상이면 종료 코드 1, 아니면 0.
- 출력 파일 쓰기는 임시 파일에 쓴 뒤 `replace`로 교체한다(중간에 멈춰도 반쯤 쓴 JSON이 남지 않게).

### 2. 표 단위 실패 표시 — `schema.py` 확장 (step 3·4·7이 쓴다)

`tables.cells_to_grid`는 셀이 겹치면 `ValueError`를 낸다. 실제 파서 출력(Docling 병합 셀 반복, 깨진 HTML)에서 날 수 있으므로, 표 하나의 실패가 문서 전체 실패가 되지 않게 표시 필드를 둔다(FP-030).

- `ParsedTable`에 `invalid_reason: str | None = None` 필드를 **마지막에 기본값과 함께** 추가한다. `None`이면 정상 표, 문자열이면 격자화할 수 없는 표(예: `"cell_overlap: ValueError: ..."`, 200자 이내). invalid 표도 원래 셀은 `cells`에 그대로 남긴다(디버깅용).
- `to_json`/`from_json`에 반영한다. 키가 없는 기존 JSON은 `None`으로 읽는다(하위 호환, `SCHEMA_VERSION`은 1 유지). 왕복 테스트를 추가한다.
- `schema.py`에 공용 헬퍼를 둔다: `build_table(page, block_position, cells) -> tuple[ParsedTable, str]` — `cells_to_grid`를 시도해 성공하면 정상 표와 step 1 규칙의 표 블록 텍스트를, `ValueError`면 `invalid_reason`을 채운 표와 대체 텍스트(anchor `(row, col)` 순으로 셀 텍스트를 `\t`로 이은 것)를 돌려준다. 어댑터는 이 헬퍼만 쓴다.
- `content_stats`에 `invalid_tables`(invalid 표 수)를 추가한다.
- 실행 기록(`parse_runs.jsonl`)에 `invalid_tables`를 추가한다. invalid 표가 있어도 문서 `status`는 `ok`다.
- 테스트: 겹치는 셀을 가진 표 1개 + 정상 표 1개로 `build_table`을 두 번 부른 문서 → 직렬화·역직렬화 후 `invalid_tables == 1`, 정상 표는 그대로.

### 3. Make 타깃

- `Makefile`: `parse-pypdf: uv run python -m kodocs.parsing.run --parser pypdf`. `.PHONY`에 추가. `verify`는 건드리지 않는다.
- `AGENTS.md` 명령어 절: `make parse-pypdf     # M2 pypdf 기준선 파싱 (로컬, 네트워크 없음), 사용자 실행`

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.parsing.run --help
grep -n "parse-pypdf" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 테스트가 표 1개 invalid → 문서 `ok` + `invalid_tables=1` 기록, 1건 실패 후 계속, 상한 초과 시 전체 중단, 설정 해시 기준 건너뛰기, 진행 출력 flush, 빈 텍스트 `empty` 판정을 모두 다루는가?
   - `parse_runs.jsonl`에 원문 텍스트가 없는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make parse-pypdf`나 `python -m kodocs.parsing.run`을 실제 데이터로 실행하지 마라(`--help` 제외). 이유: `data/manifest/parse_runs.jsonl`이 생겨 작업 트리가 바뀐다(FP-042). 실제 실행은 사용자가 한다.
- 입력 하나의 실패로 배치 전체를 멈추지 마라. 이유: FP-030. 단 `BudgetExceededError`는 전체 중단이다.
- 건너뛰기 판단을 파일명·대상 순번으로 하지 마라. 이유: FP-033(멱등성 키와 출력 키 불일치). `(parser, input_sha256)` + `settings_hash`로 한다.
- `parse_runs.jsonl`을 줄 단위로 그대로 집계하지 마라. 이유: FP-032. `latest_records`로 읽는다.
- 대상 0건을 성공으로 끝내지 마라. 이유: FP-031.
- 기존 테스트를 깨뜨리지 마라.
