# Step 2: annex-mapping

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/parsing/articles.py` — `split_document`, `AnnexMarker`, `ANNEX_RE`, `law_to_lines`
- `src/kodocs/parsing/schema.py` — `ParsedDocument`, `ParsedTable`, `from_json` 등 직렬화
- `src/kodocs/parsing/tables.py` — `cells_to_grid`, `grid_to_markdown`
- `data/manifest/parse_targets.json`, `data/manifest/parse_runs.jsonl`, `data/manifest/table_golden.json` — 파싱 대상 115개, 표 정답의 `id`(예: `17229_0-00`)와 `annex_label`(예: `<별표2>`)
- `docs/results/2026-09-27-m2-parsing.md` — M2 파서 비교와 표 결과
- `.claude/skills/failure-patterns/SKILL.md`는 읽지 않아도 된다(아래에 필요한 항목을 옮겨 적었다)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

실제 규정 조합본(pypdf 조문 + 업스테이지 별표 표)을 만들기 위한 **별표 매핑 규칙**을 만들고 실제 데이터 115개 대상 전체에 돌려 확정한다. 신규 파일: `src/kodocs/parsing/annex_mapping.py`, `tests/parsing/test_annex_mapping.py`.

### 배경

- 조문 청크는 pypdf 텍스트에서, 별표는 업스테이지 표에서 만든다. pypdf 텍스트의 별표 영역은 본문에서 제외한다.
- 골드 표 문항 qrels의 `annex_id`는 표 정답 id(`17229_0-00` 형식)이며, `data/manifest/table_golden.json`의 항목에서 `annex_label`(`<별표2>`)과 규정 번호(`rule_no`)로 이어진다. 청킹 쪽 별표 식별자는 라벨을 정규화한 값(예: `별표2`, 별표의 가지번호는 `별표2의1`)으로 통일한다.
- 로컬에 있는 파싱 결과(`data/interim/parsed/pypdf/`, `data/interim/parsed/upstage/`)를 읽는다. 대상 목록은 `parse_targets.json`의 targets(파일 해시 → 파싱 결과 경로 규칙은 `src/kodocs/parsing/run.py`의 `_current_output`와 `load_inputs`를 읽고 그대로 따른다).

### 시그니처(내부 구현은 재량)

```python
@dataclass(frozen=True)
class AnnexRegion:      # pypdf 문서의 별표 영역
    annex_id: str       # 정규화 별표 id (예: "별표2")
    kind: str           # 별표·별지·서식·부록
    label: str
    block_start: int
    block_end: int
    text: str           # pypdf 별표 영역 원문(정제 없음)

def annex_regions(pypdf_doc: ParsedDocument) -> list[AnnexRegion]: ...
def article_region_text(pypdf_doc: ParsedDocument) -> str: ...   # 별표 영역을 제외한 조문 영역 텍스트

@dataclass(frozen=True)
class AnnexTable:
    annex_id: str
    tables: tuple[ParsedTable, ...]   # 업스테이지 표 (원본 순서)
    text: str                         # `[별표N]` 라벨 + 표 마크다운 격자(grid_to_markdown) 이어 붙임

@dataclass(frozen=True)
class AnnexMapping:
    matched: dict[str, AnnexTable]      # annex_id -> 업스테이지 표
    fallback: list[str]                 # 업스테이지 표 매핑 실패 → pypdf 별표 텍스트로 대체할 annex_id
    unmapped_tables: list[int]          # 어느 별표에도 붙지 못한 업스테이지 표 인덱스

def map_annex_tables(pypdf_doc, upstage_doc) -> AnnexMapping: ...
```

매핑 규칙 초안: 업스테이지 블록을 순서대로 훑으며 `ANNEX_RE`에 맞는 라벨 블록이 나오면 현재 별표를 갱신하고, 그 뒤의 `table` 블록은 다음 별표 라벨·부칙·조 머리 전까지 현재 별표에 붙인다. **이 초안은 확정 규칙이 아니다** — 실제 데이터로 재고 고친다.

### FP-016 절차 (필수)

1. 규칙을 합성 fixture로만 정하지 마라. 115개 전체에 돌려 다음을 센다: 별표 라벨 수(pypdf 기준), 업스테이지 표 수, 매핑 성공 별표 수, 매핑 실패(`fallback`) 별표 수, 어느 별표에도 붙지 못한 표 수. 표 없는 별지·서식은 "표 없음(정상)"으로 분리해 센다.
2. 실패 사례를 유형별로 분류(라벨 표기 변형, 라벨 블록 누락, 표가 라벨 앞에 옴 등)하고 규칙을 고친 뒤 다시 센다. 표기 변형별 개수를 manifest에 남긴다.
3. 확정 후에도 남은 실패 별표는 `fallback`으로 처리하고 목록(doc, annex_id, 사유)을 남긴다.
4. 골드 표 qrels 6문항의 `annex_id`(표 정답 id → 별표 라벨 → 정규화 별표 id → 매핑 결과)가 **모두 매핑 성공(업스테이지 표 사용)**인지 별도로 확인해 manifest에 기록한다. 하나라도 fallback이면 목록과 함께 `blocked`로 보고한다(사람 판단 필요).

### 산출

- CLI: `python -m kodocs.parsing.annex_mapping` (네트워크 없음, 로컬 파일만) → `data/manifest/annex_mapping.json`이 아니라 **`data/manifest/m5_annex_mapping.json`** (커밋 대상). 내용: 규칙 버전, 대상 수, 위 집계(개수만), 실패·fallback 목록(id·사유만, 표·조문 원문 금지), 골드 표 qrels 6건 확인 결과, 입력 해시 요약. 진행 상황을 `flush=True`로 출력한다(FP-034).
- 이 step에서 위 CLI를 로컬에서 한 번 실행해 manifest를 생성한다.
- 테스트는 합성 소형 fixture(가짜 `ParsedDocument`)로 규칙 각 분기를 검증한다. 실제 파싱 결과에 의존하는 테스트는 만들지 마라(파일이 없는 환경에서도 `make verify`가 통과해야 한다). 실제 데이터 실행은 위 CLI 실행으로만 한다.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.parsing.annex_mapping
test -f data/manifest/m5_annex_mapping.json
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 네트워크 요청·다운로드·패키지 설치를 하지 마라. 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- LLM·외부 API를 호출하거나 `anthropic`·`openai`·`google.genai`·`kodocs.llm`을 import하지 마라. 이유: M5는 결정론 검색 실험 phase이며 LLM 호출이 생기면 멈추고 보고하는 조건이다.
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `src/kodocs/retrieval/`에서 `langchain`·`langgraph`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 (상대 import 포함) import하지 마라. 이유: 검색은 측정 대상이라 추상화 없이 OpenSearch·SQLite를 직접 호출한다(ADR-009).
- 기존 테스트를 깨뜨리지 마라
- 매핑 실패 건수를 세기 전에 규칙을 확정하지 마라. 이유: 합성 예시로 정한 경계 규칙이 실제 표기 변형을 놓쳤던 실패다(FP-016). 실패 유형별 개수를 manifest에 남긴다.
- `annex_regions`가 반환하는 pypdf 별표 텍스트와 업스테이지 표 텍스트를 정제·정규화하지 마라. 이유: 정제가 원문을 조용히 바꾼 실패다(FP-020). 라벨 접두사는 별도 필드로만 붙인다.
- 어떤 별표가 매핑에 실패했을 때 예외로 전체를 중단하지 마라. 이유: 항목 하나의 실패가 배치 전체를 멈춘 실패다(FP-030). fallback으로 처리하고 목록에 남긴다. 단 매핑 성공이 0건이면 실패로 멈춘다(FP-031).
- 조문·표 원문을 manifest에 넣지 마라(AGENTS.md: interim 원문 비커밋).
- 파싱 결과가 없는 대상 때문에 분모가 흔들리지 않게 하라. 이유: 정답·입력이 없는 대상이 분모에 들어간 실패다(FP-021). 대상 수는 "parse_targets의 role=current 95 + history 20 중 pypdf·upstage 결과가 둘 다 있는 문서 수"처럼 무엇을 세는지 manifest에 명시한다(FP-043).
