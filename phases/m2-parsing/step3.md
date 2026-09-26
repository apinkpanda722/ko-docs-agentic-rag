# Step 3: docling-adapter

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/ADR.md` — ADR-008(Docling은 HWP 미지원 → 변환 PDF 입력), ADR-014
- `pyproject.toml` — 의존성 그룹, `Makefile`
- step 1 산출물: `src/kodocs/parsing/schema.py`, `src/kodocs/parsing/tables.py`(`TableCell`, `cells_to_grid`)
- step 2 산출물: `src/kodocs/parsing/run.py`(`Adapter` 프로토콜, `ADAPTERS`), `tests/parsing/test_run.py`

## 배경

Docling은 로컬에서 레이아웃·표 구조 모델로 PDF를 파싱한다. **첫 실행 시 모델을 내려받고 torch 등 대용량 의존성이 필요하다.**
그래서 docling을 기본 의존성에 넣지 않고 별도 의존성 그룹에 둔다. `make verify`는 docling 없이 통과해야 한다.
모델 다운로드와 실제 파싱은 사용자가 실행한다. 이 step은 어댑터와 mock 테스트까지만 만든다.

사용할 Docling API(공식 문서 기준, context7 `/docling-project/docling`로 확인함):
- 설정: `from docling.datamodel.pipeline_options import PdfPipelineOptions`; `PdfPipelineOptions()`에 `do_ocr = False`, `do_table_structure = True`, `table_structure_options.do_cell_matching = True`
- 변환기: `from docling.document_converter import DocumentConverter, PdfFormatOption`; `from docling.datamodel.base_models import InputFormat`;
  `DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})`, `converter.convert(path).document`
- 문서 순회: `doc.iterate_items(included_content_layers=...)`는 `(item, level)`을 준다. 본문(`body`) 외에 머리말·꼬리말은 `furniture` 층에 있다.
  항목 속성: `item.label`(`DocItemLabel`: `title`, `section_header`, `text`, `list_item`, `page_header`, `page_footer`, `caption`, `table`, …), `item.prov[0].page_no`, `item.text`
- 표: `TableItem.data.table_cells`의 각 셀 `start_row_offset_idx`, `start_col_offset_idx`, `row_span`, `col_span`, `text`
- 모델 사전 다운로드 CLI: `docling-tools models download`

## 작업

TDD: `tests/parsing/test_docling_adapter.py`를 먼저 작성한다. **docling을 import하지 않는 가짜 객체**(label·prov·text·data.table_cells를 가진 dataclass)로 변환 결과를 흉내 낸다.

### 1. 의존성 그룹

- `uv add --group docling --no-sync docling`으로 `pyproject.toml`의 `[dependency-groups]`에 `docling` 그룹을 추가하고 `uv.lock`을 갱신한다.
  `--no-sync`로 설치는 하지 않는다(torch 포함 대용량). `make verify`(`uv run`)는 기본 그룹만 쓰므로 docling 없이 돈다.
- lock 갱신이 네트워크 문제로 실패하면 `"status": "blocked"`로 사유를 보고한다. 버전을 추측해 손으로 적지 마라.

### 2. `src/kodocs/parsing/docling_adapter.py`

```python
PARSER_NAME = "docling"

@dataclass(frozen=True)
class DoclingSettings:
    do_ocr: bool = False
    do_table_structure: bool = True
    do_cell_matching: bool = True
    # 장치는 기본값(자동)을 쓰고, 실제 사용 장치를 알 수 있으면 settings에 기록한다

class DoclingAdapter:
    name = PARSER_NAME
    def __init__(self, settings: DoclingSettings = DoclingSettings(), *, converter_factory: Callable[[DoclingSettings], object] | None = None): ...
    def settings(self) -> Mapping[str, object]: ...   # 위 옵션 + docling/docling-core/torch 버전(importlib.metadata, 없으면 "not-installed")
    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument: ...

def document_to_parsed(doc: object, *, item: ParseInput, root: Path, settings: Mapping[str, object], elapsed_s: float) -> ParsedDocument: ...
```

- **docling import는 `converter_factory` 기본 구현 함수 안에서만** 한다(모듈 최상위 import 금지). docling이 없으면 "docling 그룹이 설치되지 않았다: `uv sync --group docling` 후 `make docling-models`"라는 `ParseError`를 낸다.
- 변환기는 어댑터 인스턴스당 한 번만 만든다(파일마다 모델을 다시 올리지 않게).
- 라벨 → `BlockKind` 매핑: `title`·`section_header` → `heading`, `text`·`paragraph` → `text`, `list_item` → `list_item`, `page_header` → `header`, `page_footer` → `footer`, `caption` → `caption`, `table` → `table`, 나머지 → `other`.
  `furniture` 층 항목도 포함해 순서대로 넣는다(머리말 잡음 제거는 분할기가 라벨을 보고 한다. 파서가 머리말을 구분하는 능력도 측정 대상이다).
- 표: `table_cells`를 `TableCell(row=start_row_offset_idx, col=start_col_offset_idx, row_span, col_span, text)`로 옮긴다. 같은 anchor가 중복으로 나오면 한 번만 넣는다. **격자화는 `tables.cells_to_grid`만 쓴다.** 표 블록 텍스트도 step 1 규칙(격자 기준 `\t`/`\n`)으로 만든다.
- 쪽 번호가 없는 항목(`prov` 비어 있음)은 직전 항목의 쪽을 쓴다.
- docling 변환 예외는 `ParseError`로 감싸 원인 타입 이름을 남긴다(FP-011). 실행기가 기록 후 계속한다.
- `run.ADAPTERS`에 `"docling"`을 등록한다.

### 3. Make 타깃

- `docling-models: uv run --group docling docling-tools models download` — 레이아웃·표 모델 다운로드(네트워크, 사용자 실행)
- `parse-docling: uv run --group docling python -m kodocs.parsing.run --parser docling`
- `.PHONY`에 추가. `AGENTS.md` 명령어 절에 두 줄 추가:
  `make docling-models  # Docling 그룹 설치(torch 포함 대용량)·모델 다운로드 — 네트워크, 사용자 실행`,
  `make parse-docling   # M2 Docling 파싱 (로컬, 시간이 오래 걸릴 수 있음), 사용자 실행`

## Acceptance Criteria

```bash
make verify
grep -n "docling" pyproject.toml Makefile AGENTS.md
uv run python -c "import kodocs.parsing.docling_adapter as m; from kodocs.parsing.run import ADAPTERS; assert 'docling' in ADAPTERS; print(m.PARSER_NAME)"   # docling 미설치 상태에서도 import 성공
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - `make verify`가 docling 미설치 상태에서 통과하는가? 테스트가 docling을 import하지 않는가?
   - 표 격자화가 `cells_to_grid`를 거치는가?
   - settings에 버전·OCR 끔·표 구조 옵션이 기록되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `uv sync --group docling`, `make docling-models`, `make parse-docling`을 실행하지 마라. Docling 모델을 내려받거나 실제 PDF를 파싱하지 마라. 이유: FP-040(지시에 없는 다운로드·설치 금지), 모델 다운로드와 대용량 설치는 사용자가 승인·실행한다.
- docling을 기본 `dependencies`나 `dev` 그룹에 넣지 마라. 이유: `make verify`·CI가 torch를 설치하게 된다.
- 모듈 최상위에서 docling을 import하지 마라. 이유: 미설치 환경에서 `make verify`의 수집 단계가 깨진다.
- OCR을 켜지 마라. 이유: 변환 PDF에는 텍스트 층이 있고, OCR을 켜면 파서 간 비교 조건이 달라진다. 설정은 settings에 기록한다.
- docling 속성 이름을 추측으로 늘리지 마라(위 목록만 쓴다). 실제 버전과 다르면 사용자 실행에서 `ParseError`로 드러나고 기록된다.
- 기존 테스트를 깨뜨리지 마라.
