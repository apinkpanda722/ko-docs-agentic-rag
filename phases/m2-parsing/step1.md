# Step 1: parse-schema

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`
- `docs/ARCHITECTURE.md` — "파서·청커 교체 가능" 패턴, `data/interim/`
- `docs/ADR.md` — ADR-007(조문 분할은 파서와 독립), ADR-008(파서 비교는 같은 PDF 입력)
- `docs/EVAL.md` — 파싱 지표
- `data/manifest/parse_targets.json` — 대상 필드(`pdf_path`, `source_sha256`, `source_kind`, `page_count`, `law_rule_id`, `law_serial`). 원문이 없는 메타데이터라 읽어도 된다.
- `src/kodocs/ingest/convert.py` — pypdf로 `hangul_distinct`를 세는 방식(FP-001)
- `src/kodocs/ingest/manifest.py` — `sha256_file`
- step 0 산출물: `src/kodocs/costs.py`

## 배경

파이프라인은 `변환 PDF → [파서 어댑터] → 공통 파싱 결과 → [조문 분할기] → [채점]`이다. **파서만 바뀌고 나머지는 같아야 한다.**
이 step은 공통 파싱 결과 스키마, 표 셀 공통 함수, 기준선 pypdf 어댑터를 만든다. Docling·업스테이지 어댑터는 step 3·4, 실행 CLI는 step 2다.

## 작업

TDD: `tests/parsing/test_schema.py`, `tests/parsing/test_tables.py`, `tests/parsing/test_pypdf_adapter.py`를 먼저 작성한다.
pypdf 테스트용 PDF는 테스트 안에서 만든다(`pypdf`로 빈 쪽 PDF, 손상 바이트 등). 실제 `data/raw/` PDF를 테스트에 쓰지 마라.

### 1. `src/kodocs/parsing/schema.py`

```python
SCHEMA_VERSION: int = 1
BlockKind = Literal["text", "heading", "list_item", "header", "footer", "page_number", "table", "caption", "other"]
InputKind = Literal["pdf", "hwp5", "hwpx"]

@dataclass(frozen=True)
class Block:
    page: int        # 1부터
    kind: BlockKind  # 파서가 붙인 라벨을 옮긴 것. 라벨이 없는 파서(pypdf)는 "text"
    text: str        # 파서 출력 그대로. 정규화하지 않는다.

@dataclass(frozen=True)
class TableCell:
    row: int         # 0부터, 병합 셀은 왼쪽 위(anchor) 위치
    col: int
    row_span: int    # >= 1
    col_span: int    # >= 1
    text: str

@dataclass(frozen=True)
class ParsedTable:
    page: int              # 표가 시작하는 쪽
    block_position: int    # blocks 안에서 이 표에 해당하는 kind="table" 블록의 인덱스
    cells: tuple[TableCell, ...]

@dataclass(frozen=True)
class ParsedDocument:
    schema_version: int
    parser: str             # "pypdf" | "docling" | "upstage" | "upstage-direct"
    parser_version: str
    settings: Mapping[str, object]   # 파서 설정·요청 옵션 전부 (재현용)
    settings_hash: str               # settings를 키 정렬 JSON으로 직렬화한 sha256 앞 12자
    input_path: str                  # 저장소 상대 경로
    input_sha256: str
    input_kind: InputKind
    page_count: int | None
    blocks: tuple[Block, ...]
    tables: tuple[ParsedTable, ...]
    elapsed_s: float                 # 파싱 벽시계 시간 (캐시 재사용 시 최초 호출 시간)

def to_json(doc: ParsedDocument) -> str: ...
def from_json(text: str) -> ParsedDocument: ...
def output_path(root: Path, parser: str, input_sha256: str) -> Path: ...
    # data/interim/parsed/{parser}/{input_sha256}.json
```

- 표가 있는 파서는 표 위치에 `kind="table"` 블록을 하나 넣는다. 그 블록의 `text`는 셀 텍스트를 행 순서·열 순서로 이은 것(행 사이 `\n`, 셀 사이 `\t`)이다. 조문 안에 표가 있어도 분할기가 텍스트를 잃지 않게 하기 위해서다.
- JSON 왕복(to_json → from_json)이 동일해야 한다. 한글은 `ensure_ascii=False`.
- `settings_hash` 계산 함수를 공개 함수로 둔다(`settings_hash(settings) -> str`). 키 순서가 달라도 같은 해시.

### 2. `src/kodocs/parsing/tables.py` — 병합 셀 복제 공통 함수 (유일한 구현)

```python
def cells_to_grid(cells: Sequence[TableCell]) -> list[list[str]]: ...
def place_html_rows(rows: Sequence[Sequence[tuple[str, int, int]]]) -> list[TableCell]: ...
    # HTML처럼 위치가 없는 형식: 행마다 (text, rowspan, colspan) 목록 → 앞 행의 rowspan이 차지한 칸을 건너뛰며 anchor 좌표 계산
def grid_to_markdown(grid: Sequence[Sequence[str]]) -> str: ...
def markdown_to_grid(text: str) -> list[list[str]]: ...
```

- **병합 셀 규칙: 병합 셀의 텍스트를 그 셀이 덮는 모든 위치에 복제한다.** 이 규칙은 `cells_to_grid` 한 곳에만 구현한다.
  golden 초안(HWPX, step 7), Docling span(step 3), 업스테이지 rowspan/colspan(step 4) 변환은 모두 `TableCell`까지만 만들고 격자화는 이 함수를 쓴다.
- 격자 크기 = max(row+row_span) × max(col+col_span). 아무 셀도 덮지 않는 위치는 `""`.
- 셀이 겹치면(두 anchor가 같은 위치를 덮음) `ValueError`.
- 마크다운: 첫 행을 헤더 행으로 쓰고 구분선 `|---|`을 넣는다. 셀 안의 `|`는 `\|`, 줄바꿈은 공백 하나로 바꾼다. `markdown_to_grid(grid_to_markdown(g)) == g`(줄바꿈 없는 셀 기준) 왕복 테스트.
- 테스트: 같은 논리 표(2×2 병합 포함)를 (a) anchor+span 셀 목록, (b) HTML 행 목록(`place_html_rows`)으로 표현했을 때 `cells_to_grid` 결과가 같다.

### 3. `src/kodocs/parsing/pypdf_adapter.py` — 기준선

```python
PARSER_NAME = "pypdf"
def parse_pdf(pdf_path: Path, *, root: Path) -> ParsedDocument: ...
```

- `PdfReader(pdf_path).pages[i].extract_text()`를 쪽마다 호출한다. 추출 모드는 기본값(plain)으로 고정하고 `settings`에 `{"extraction_mode": "plain"}`을 기록한다.
- 쪽마다 비어 있지 않은 줄을 하나씩 `Block(page, "text", line)`으로 만든다. 줄 앞뒤 공백 외에는 텍스트를 바꾸지 않는다.
- pypdf는 표를 추출하지 않는다. `tables=()`. (채점에서 "해당 없음"으로 보고한다.)
- `parser_version`은 `importlib.metadata.version("pypdf")`.
- 손상 PDF: pypdf가 던지는 예외를 **손상 바이트를 실제로 넣어** 확인하고, 어댑터 경계에서 `ParseError(RuntimeError)`(schema.py에 정의)로 감싸 원인 예외 타입 이름을 메시지에 남긴다. 회귀 테스트를 둔다(FP-011).

### 4. 내용 지표 함수 (FP-001)

`schema.py`에 `content_stats(doc) -> dict`: `text_chars`(블록 텍스트 총 글자 수), `hangul_distinct`(서로 다른 한글 음절 수), `blocks`, `tables`.
step 2가 실행 기록에 넣는다. 형식만 보고 성공 판정하지 않기 위해서다.

## Acceptance Criteria

```bash
make verify
uv run python -c "from kodocs.parsing.schema import ParsedDocument, SCHEMA_VERSION; from kodocs.parsing.tables import cells_to_grid; from kodocs.parsing.pypdf_adapter import parse_pdf; print(SCHEMA_VERSION)"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가? (`src/kodocs/parsing/`)
   - 어댑터가 텍스트를 정규화하지 않는가?
   - 병합 셀 복제가 `cells_to_grid` 한 곳에만 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 어댑터에서 텍스트를 정규화(공백 합치기, 전각→반각, 따옴표 통일, 주석 제거)하지 마라. 이유: FP-020(정제가 정답 비교 텍스트를 조용히 바꿈). 정규화는 채점(step 6)에서만, 문서화된 규칙으로 하고 원문 비교와 분리 보고한다.
- 병합 셀 복제 로직을 `tables.py` 밖에 다시 구현하지 마라. 이유: 표현 형식마다 규칙이 달라지면 셀 일치율이 파서가 아니라 변환 코드 차이를 잰다.
- 실제 `data/raw/pdf/` 파일을 파싱하거나 `data/interim/parsed/`에 쓰지 마라. 이유: 실행은 사용자가 한다(ADR-014), FP-042. 테스트는 `tmp_path`만 쓴다.
- 파서 예외를 `except Exception: pass`로 삼키지 마라. 이유: FP-011. 어댑터 경계에서 `ParseError`로 감싸 원인을 남기고, 계속 진행 여부는 step 2 실행기가 정한다.
- 기존 테스트를 깨뜨리지 마라.
