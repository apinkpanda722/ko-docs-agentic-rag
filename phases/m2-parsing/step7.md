# Step 7: table-golden

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL: `data/golden/`은 사람이 검수해 확정한 기준, 에이전트는 수정하지 않는다
- `docs/EVAL.md` — 표 셀 일치율(별표 표 약 10개, 사람 검수 정답과 셀 단위 비교)
- `docs/ADR.md` — ADR-002(원문 재배포 금지), ADR-008
- `docs/results/2026-09-26-m1-data-collection.md` — 6절 3항(HWPX `Contents/section*.xml`, `hp:t`, `hp:tbl`)
- `src/kodocs/ingest/filetype.py` — HWPX 판정(`mimetype` 확인, FP-010)
- `data/manifest/parse_targets.json` — `source_kind == "hwpx"` 대상 10개, `law_annex_count`
- step 1: `src/kodocs/parsing/tables.py` (`TableCell`, `cells_to_grid`, `grid_to_markdown`, `markdown_to_grid`)
- step 5: `src/kodocs/parsing/articles.py` (별표 표기 정규식 상수)
- step 6: `src/kodocs/eval/text_normalize.py`, `src/kodocs/eval/parsing_score.py`
- `.gitignore`

## 배경

표 정답은 **특정 파서의 출력으로 만들지 않는다**(그 파서에 유리해진다). HWPX 원본의 표 XML(`hp:tbl`)에서 파서 중립 초안(마크다운)을 뽑고,
사람이 약 10개를 골라 검수해 `data/golden/tables/`에 확정한다. 법제처 `annexes`에는 제목·링크만 있고 표 내용이 없어서 HWPX가 유일한 중립 원천이다.

실측(parse_targets의 HWPX 10개): 법제처 `kind=별표` 항목은 약 12개(규정 47161, 52921, 53959, 54082, 17229)이고 나머지는 별지·서식이다.
원본 표기는 `[별표 1]`, `【 별표 1 】`, `[별지 제1호서식]` 등으로 섞여 있다.

**저장·커밋 결정**: 표 셀은 규정 원문이므로 `data/golden/tables/`는 **gitignore**한다(ADR-002). 대신 정답 선정 정보를 manifest로 커밋해 재현 가능하게 한다.

## 작업

TDD: `tests/eval/test_table_drafts.py`, `tests/eval/test_table_score.py`를 먼저 작성한다. HWPX는 테스트 안에서 zip으로 만든다(`mimetype`, `Contents/section0.xml`에 `hp:tbl`/`hp:tr`/`hp:tc`/`hp:cellAddr`/`hp:cellSpan`/`hp:subList`/`hp:p`/`hp:run`/`hp:t` 최소 구조).

### 1. `src/kodocs/eval/table_drafts.py` — HWPX 표 → 초안

```python
@dataclass(frozen=True)
class TableDraft:
    rule_no: str
    source_sha256: str
    pdf_sha256: str | None      # parse_targets의 pdf 파일 sha256 (채점 대상 연결용, 파일을 읽어 계산)
    section: str                # "Contents/section0.xml"
    table_ordinal: int          # 그 섹션 안 최상위 hp:tbl 순번(0부터, 문서 순서)
    annex_label: str | None     # 표 앞에서 가장 가까운 별표·별지 표기(원문 그대로), 없으면 None
    annex_kind: str | None      # "별표" | "별지" | "서식" | "부록"
    rows: int
    cols: int
    nested: bool                # 셀 안에 표가 있으면 True (초안 품질 경고)
    cells: tuple[TableCell, ...]

def extract_hwpx_tables(path: Path) -> list[TableDraft]: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.eval.table_drafts
```

- HWPX 판정은 확장자가 아니라 zip 안 `mimetype`으로 한다(FP-010, `ingest/filetype.py` 재사용).
- 셀 위치는 `hp:cellAddr`의 `rowAddr`/`colAddr`, 병합은 `hp:cellSpan`의 `rowSpan`/`colSpan`에서 읽는다. 셀 텍스트는 그 셀의 `hp:subList` 안 문단(`hp:p`)마다 `hp:t` 텍스트를 이어 붙이고 문단 사이는 공백 하나로 잇는다. 셀 안의 중첩 표 텍스트는 그 셀 텍스트에 포함하되 `nested=True`.
- **격자·마크다운은 `tables.cells_to_grid`와 `grid_to_markdown`만 쓴다**(병합 셀 복제 규칙을 여기서 다시 구현하지 마라).
- `annex_label`: 섹션 XML을 문서 순서로 훑으며 `hp:t` 텍스트가 step 5의 별표 표기 정규식에 맞으면 현재 표기로 기억한다. 표는 가장 최근 표기를 가진다.
- 손상 zip·XML(`BadZipFile`, `ParseError`)은 그 파일만 실패로 출력하고 계속한다. 실제 예외 타입을 손상 입력으로 확인해 테스트로 남긴다(FP-011).
- CLI 동작:
  - 입력: `parse_targets.json`의 `source_kind == "hwpx"` 대상.
  - 출력: `data/interim/table_drafts/{rule_no}/{section 번호}-{table_ordinal:02d}.md`(커밋 안 함). 파일 첫머리에 아래 **front matter**, 그 뒤에 마크다운 표.
    ```
    ---
    rule_no: "47161"
    source_sha256: "…"
    pdf_sha256: "…"
    section: "Contents/section0.xml"
    table_ordinal: 3
    annex_label: "[별표 1] 단말기 단계별 보안관리 관련 규정"
    annex_kind: "별표"
    shape: [12, 4]
    nested: false
    draft_generator: "kodocs.eval.table_drafts"
    ---
    ```
  - `data/interim/table_drafts/candidates.md`: 전체 후보 목록 표(rule_no, 파일, annex_label, 크기, nested). 정렬은 `annex_kind == "별표"` 먼저, 그다음 `별지`·`서식`, 표기 없음은 마지막. 사람이 이 목록에서 약 10개를 고른다.
  - 진행 줄을 파일마다 `flush=True`로 출력(FP-034).

### 2. 정답 선정 manifest — `src/kodocs/eval/table_golden.py`

```python
def index_golden(root: Path) -> dict: ...   # data/golden/tables/*.md를 읽어 선정 정보를 만든다 (golden 파일은 읽기만)
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.eval.table_golden → data/manifest/table_golden.json
```

- 사람이 검수한 파일은 `data/golden/tables/{임의 id}.md`에 초안과 같은 front matter + 마크다운 표 형식으로 둔다. `reviewed_by`, `reviewed_on` 키를 사람이 추가한다(없으면 경고).
- `data/manifest/table_golden.json`(커밋 대상, **셀 텍스트 없음**): 정답마다 `id`, `rule_no`, `source_sha256`, `pdf_sha256`, `section`, `table_ordinal`, `annex_label`, `shape`, `golden_file_sha256`(md 파일 바이트 sha256), `reviewed_by`, `reviewed_on`.
- 정답이 0개면 실패로 멈추고 `make table-drafts` → 검수 → 복사 절차를 안내한다(FP-031).

### 3. 표 셀 채점 — `src/kodocs/eval/table_score.py`

```python
@dataclass(frozen=True)
class TableScore:
    golden_id: str
    parser: str
    applicable: bool            # 파서가 표 추출 기능이 없으면 False (pypdf)
    status: str                 # ok | no_tables | parse_missing | golden_changed
    best_table_index: int | None
    shape_match: bool
    cell_exact_rate: float | None   # golden 격자 칸 중 정규화 후 완전 일치 비율
    cell_sim_mean: float | None     # golden 격자 칸별 정규화 문자 유사도 평균

def score_table(golden_grid: list[list[str]], parsed: ParsedDocument) -> TableScore: ...
def run(root: Path, *, out: TextIO = sys.stdout) -> dict: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.eval.table_score
```

- 정답은 `data/manifest/table_golden.json`의 목록을 기준으로 읽고, 각 md 파일의 sha256이 manifest의 `golden_file_sha256`과 다르면 그 정답을 `golden_changed`로 표시하고 채점하지 않는다(검수 후 파일이 바뀌었다는 신호).
- 예측 문서: `pdf_sha256`으로 파서별 `ParsedDocument`를 찾는다.
- 예측 표 선택: 그 문서의 모든 `ParsedTable`을 `cells_to_grid`로 격자화하고, golden 격자와 **(0,0)에 맞춰 겹친 칸**의 정규화(`text_normalize.normalize`) 완전 일치 수가 가장 큰 표를 고른다. 동점이면 앞 표.
- `cell_exact_rate` 분모 = golden 격자의 모든 칸 수(빈 칸 포함). 예측 격자에 없는 칸은 불일치. `shape_match` = 행·열 수가 같음.
- **invalid 표 격리(FP-030)**: `invalid_reason`이 있는 예측 표, 또는 채점 중 `cells_to_grid`가 예외를 낸 예측 표는 **후보에서 빼고** `invalid_candidates` 수로 센다. 그 문서의 표가 전부 invalid라 후보가 0개면 그 정답은 `status="all_invalid"`, `cell_exact_rate=0.0`, `cell_sim_mean=0.0`, `shape_match=False`로 채점하고(불일치, 평균에 포함) 사유(invalid 사유 목록 요약)를 남긴다. 표 기능이 있는데 표를 하나도 내지 않은 `no_tables`와 구분한다. 어떤 경우에도 채점 실행을 멈추지 않는다.
  - `TableScore`에 `invalid_candidates: int`, `reason: str | None` 필드를 추가한다. `table_scores.json`과 요약 표에 파서별 invalid 표 수를 싣는다.
  - 테스트(필수): 예측 문서에 **겹치는 셀을 가진 표 1개 + 정상 표 1개** → 채점 성공, `invalid_candidates=1`, 정상 표로 채점. 그리고 invalid 표만 있는 문서 → `all_invalid`, 수치 0, 사유 기록, 다음 정답 채점 계속.
- 여러 쪽으로 나뉜 예측 표를 합치지 않는다(엄격 채점). 나뉜 경우는 결과의 실패 사례로 드러난다.
- **pypdf처럼 표 추출 기능이 없는 파서는 `applicable=False`, 수치 `None`으로 두고 평균에서 제외한다.** 보고 표에는 "해당 없음(표 추출 기능 없음)"으로 적는다. 파서가 표 기능이 있는데 그 문서에서 표를 하나도 내지 않았으면 `no_tables`, 수치 0으로 평균에 포함한다.
- 출력: `data/manifest/table_scores.json`(커밋, 셀 텍스트 없음 — id·수치·상태만), 표준 출력에 파서별 요약 표.

### 4. gitignore와 make 타깃

- `.gitignore`에 `data/golden/tables/*`와 `!data/golden/tables/README.md`를 추가한다.
- `data/golden/tables/README.md`를 **만들지 마라**(에이전트는 `data/golden/`을 수정하지 않는다). 보관 규칙 설명은 step 8에서 EVAL.md에 적는다.
- `Makefile`: `table-drafts: uv run python -m kodocs.eval.table_drafts`, `table-golden: uv run python -m kodocs.eval.table_golden`, `table-score: uv run python -m kodocs.eval.table_score`. `.PHONY`에 추가.
- `AGENTS.md` 명령어 절에 세 줄 추가(모두 네트워크 없음):
  `make table-drafts  # HWPX 원본 표(hp:tbl)에서 파서 중립 표 정답 초안 추출 → data/interim/table_drafts/`,
  `make table-golden  # 사람이 확정한 data/golden/tables/*.md의 선정 정보·해시를 data/manifest/table_golden.json에 기록`,
  `make table-score   # 파서별 표 셀 일치율 채점`

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.table_drafts --help
uv run python -m kodocs.eval.table_golden --help
uv run python -m kodocs.eval.table_score --help
grep -n "data/golden/tables" .gitignore
test ! -e data/golden/tables/README.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 초안이 파서 출력이 아니라 HWPX XML에서만 나오는가?
   - 병합 셀 복제가 `cells_to_grid`만 거치는가(HWPX 경로 테스트에서 같은 논리 표가 step 1 테스트와 같은 격자를 내는가)?
   - `table_golden.json`·`table_scores.json`에 셀 텍스트가 없는가(테스트)?
   - invalid 표 격리 테스트(겹치는 셀 표 1개 + 정상 표 1개 → 채점 성공·invalid 1, 전부 invalid → `all_invalid` 0점·계속)가 있는가?
   - pypdf가 평균에서 빠지고 "해당 없음"으로 표시되는가(테스트)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `data/golden/` 아래에 어떤 파일도 만들거나 수정하지 마라(README 포함). 이유: AGENTS.md CRITICAL, 사람이 검수해 확정하는 기준이다.
- 표 정답 초안을 pypdf·Docling·업스테이지 출력에서 만들지 마라. 이유: 그 파서에 유리해진다.
- `make table-drafts`·`table-golden`·`table-score`를 실데이터로 실행하지 마라. 이유: FP-042(작업 트리 변경), 원문을 담는 초안 생성과 검수는 사용자가 한다.
- 커밋 대상 파일(`table_golden.json`, `table_scores.json`, 표준 출력 요약)에 셀 텍스트를 넣지 마라. 이유: ADR-002(원문 재배포 금지). 셀 텍스트는 gitignore된 `data/golden/tables/`와 `data/interim/`에만 둔다.
- HWPX 여부를 확장자로 판정하지 마라. 이유: FP-010.
- 병합 셀 복제를 이 step에서 다시 구현하지 마라. 이유: 형식별 규칙이 달라지면 셀 일치율이 변환 코드 차이를 잰다.
- 파일 하나의 손상으로 추출 전체를 멈추지 마라. 이유: FP-030.
- 기존 테스트를 깨뜨리지 마라.
