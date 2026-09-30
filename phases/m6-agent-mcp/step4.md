# Step 4: tool-contract

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/retrieval/`의 `search.py`·`lookup.py`·`sql.py`·`document.py`(step 3)·`access.py`·`ordering.py`(step 2)·`embedding.py`(`Embedder`)·`opensearch.py`
- `data/manifest/m5_config.json` — 최종 구성 필드(`hybrid_method`·`bm25_weight`·`candidate_chunks`·`indexes`·`config_hash`), `src/kodocs/eval/m5_test.py`·`m5_runner.py`에서 이 설정을 `SearchParams`로 바꾸는 코드
- `tests/test_architecture.py` — 기존 AST 계층 가드(`find_forbidden_imports`) 구조
- `pyproject.toml`, `src/kodocs/retrieval/sql.py`의 `ALLOWED_TABLES`·`DEFAULT_LIMIT`·스키마 정보

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

`src/kodocs/tools/` 공유 계약 계층을 만든다(ADR-029 2·4·8번). 이 step에서는 `langchain`·`langgraph`·`mcp`·`openai`를 쓰지 않는 **순수 파이썬 + 이미 있는 의존성**만 쓴다. 스키마 모델에는 `pydantic`을 쓰되 **이미 설치되어 있는지 확인**하고(`uv run python -c "import pydantic"`), 없으면 `blocked`로 보고한다(사용자가 의존성 추가를 승인한다).

### 파일
- `tools/__init__.py`: 공개 이름 재노출(`run_tool`, `ToolDeps`, `ToolResult`, `TOOL_VERSIONS`, `UserContext`, 오류 코드).
- `tools/contract.py`: 도구 4개의 입력·출력 모델, 도구 설명 문자열, `TOOL_VERSIONS = {"search_docs": 1, "lookup_id": 1, "run_sql": 1, "get_document": 1}`, 오류 코드 `unauthorized | not_found | invalid_argument | limit_exceeded`, `error_payload(code, message)`.
- `tools/deps.py`: `ToolDeps`(frozen dataclass: `client`, `index`, `embedder`, `db_path`, `search_params`, `prefer_current: bool = True`, 길이 상수) 와 `load_tool_config()`(`m5_config.json`을 읽어 인덱스 `kodocs-m5-composite-structural`·`SearchParams`(hybrid_weighted, bm25_weight, candidate_chunks)를 만든다. 테스트가 이 인덱스 이름이 `m5_config.json`의 `indexes`에 있음을 확인한다). 임베더·클라이언트를 실제로 만드는 `build_tool_deps()`는 무거운 import를 함수 안에서 지연한다.
- `tools/run.py`: `run_tool(name, args, ctx, deps) -> ToolResult`. `ToolResult`는 `payload: dict`(도구 출력 또는 `{"error": {...}}`), `is_error: bool`, `refs: tuple[dict, ...]`(인용 검증·트레이스용 `{"kind":"doc","doc_id","section_key"}` 등), `timings: dict | None`(`embed_ms`·`search_ms`)을 갖는다. **payload에는 시간·토큰 같은 변동 필드를 넣지 않는다**(parity 비교용).
- `tools/shape.py`: 결과 축약(길이 상한) 함수들.

### 도구 계약(고정)
| 도구 | 입력(모델이 채우는 인자만) | 출력 payload |
|---|---|---|
| `search_docs` | `query: str`(1~500자) | `{"results": [{"doc_id","title","section_key","section_kind","is_current","text"}], "truncated": bool}` — `authorized_search(prefer_current=deps.prefer_current)` 상위 **8청크**, 청크 텍스트 합 **6,000자**(뒤 청크부터 통째로 제외, 첫 청크가 6,000자 초과면 잘라 포함) |
| `lookup_id` | `kind: Literal["ticket","approval","incident","service","regulation"]`, `value: str`(1~100자) | `{"documents": [{"doc_id","title","is_current","date","text"}], "truncated": bool}` — 최대 5문서·문서당 1,200자·직렬화 6,000자 이하 |
| `run_sql` | `sql: str`(1~2,000자) | `{"columns": [...], "rows": [[...]], "truncated": bool}` — `authorized_run_sql`(기본 LIMIT), 직렬화가 6,000자를 넘으면 행을 뒤에서부터 버리고 `truncated=true`. 도구 설명에 허용 테이블(`ALLOWED_TABLES`)과 컬럼을 넣는다 |
| `get_document` | `doc_id: str`(1~100자), `section_key: str | None`(≤100자) | `{"doc_id","title","sections":[section_key,...],"text": str,"truncated": bool}` — 청크를 `start` 순으로 이어 붙여 6,000자 상한 |

- **사용자 컨텍스트(팀·직급)는 어떤 도구의 입력 모델에도 없다.** `ctx`는 `run_tool`의 별도 인자로만 받는다(CRITICAL). **모든 입력 모델은 계약에 없는 인자를 거부한다(pydantic `extra="forbid"` → `invalid_argument`).** 모델이 `team` 같은 숨은 인자를 끼워 넣어도 무시되지 않고 오류가 되어야 한다.
- 입력 길이 상한 초과 → `limit_exceeded`, 타입·enum 위반·빈 값 → `invalid_argument`, `SqlRejected` → `invalid_argument`(메시지는 사유 코드만), `get_document`에서 `authorized_get_document`가 `None` → `not_found`(메시지 고정 문자열 `"문서를 찾을 수 없습니다."`, **없는 ID와 권한 없는 문서의 payload가 바이트 동일**). `lookup_id` 결과가 없으면 오류가 아니라 빈 `documents`. 예상 못한 예외(OpenSearch 장애 등)는 삼키지 않고 전파한다. `unauthorized`는 정의만 하고 `tools`는 만들지 않는다(MCP 인증 계층 몫).

### 계약 스냅샷·테스트
- `tests/contract/snapshots/{tool}.v{N}.json`에 입력·출력 JSON 스키마, 도구 설명, 버전을 저장하고 `tests/contract/snapshots/index.json`에 파일별 sha256을 적는다. 스냅샷 생성 스크립트/명령은 `python -m kodocs.tools.snapshot`처럼 **명시적으로 실행할 때만** 파일을 쓴다(테스트가 자동 갱신하지 않는다).
- 계약 테스트(`tests/tools/test_contract.py`): (1) 현재 스키마 해시 = 가장 높은 버전 스냅샷 해시. (2) 스키마가 바뀌었는데 `TOOL_VERSIONS`가 스냅샷보다 크지 않으면 실패, 올렸는데 새 스냅샷 파일이 없으면 실패. (3) 과거 버전 스냅샷 파일의 해시가 `index.json`과 일치. (4) 모든 입력 모델의 필드 이름에 `team`·`grade`·`user`·`ctx`·`token`이 없음. 계약에 없는 인자(`team` 등)를 넣으면 `invalid_argument`. (5) 오류 payload 형식 `{"error":{"code","message"}}`와 코드 4종. (6) 없는 `doc_id`와 권한 없는 `doc_id`(fake client가 둘 다 0건)의 `run_tool` payload가 `json.dumps(sort_keys=True)` 기준 바이트 동일. (7) `search_docs`가 8청크·6,000자 상한을 지킴(경계 케이스: 첫 청크 6,001자, 8번째 청크에서 초과). (8) `prefer_current=True`가 실제 `authorized_search`에 전달됨.
- 계층 가드(`tests/test_architecture.py` 확장, 기존 `find_forbidden_imports` 재사용): `retrieval`은 `kodocs.tools`도 import 금지(기존 목록에 추가). `tools`는 `kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`·`kodocs.llm`·`langchain*`·`langgraph*`·`mcp`·`mcp.*`·`openai`·`anthropic` 금지. `kodocs.agent`·`kodocs.mcp_server`는 `kodocs.retrieval*`을 import 금지(`tools`를 통해서만; 두 패키지가 아직 비어 있어도 가드는 만든다). 위반 예시 fixture로 검사기를 테스트한다.
- `tests/test_architecture.py`의 M5 LLM 미호출 가드 대상에 `tools/**`를 추가한다.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/tools tests/test_architecture.py
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - 도구 입력 모델 어디에도 팀·직급·사용자 인자가 없는가(모델이 권한 값을 보거나 만들 수 없는가)?
   - 권한 없는 문서와 없는 문서의 응답이 바이트 동일한가?
   - 스키마가 바뀌면 버전 상승을 강제하는 테스트가 실제로 실패를 감지하는가(fixture로 검증)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `tools/`에서 OpenSearch 쿼리를 직접 만들거나 권한 조건을 다시 구현하지 마라. 이유: 권한은 `retrieval.authorized_*` 한 곳에만 둔다(ADR-010). `tools`는 그 함수들을 부르는 얇은 층이다.
- 테스트가 스냅샷 파일을 자동 생성·갱신하게 하지 마라. 이유: 호환되지 않는 변경이 조용히 통과한다(계약 테스트가 무력화됨).
- `ToolResult.payload`에 지연·토큰·타임스탬프 등 실행마다 달라지는 값을 넣지 마라. 이유: in-process와 MCP 결과 동일성(parity, 규칙 19)을 바이트로 비교한다. 변동 값은 `timings`에만 둔다.
- 새 의존성을 `uv add`로 추가하지 마라(pydantic이 없으면 `blocked`). 이유: 의존성 추가는 승인된 step(5·7·9)에서만 한다(FP-040).
- `retrieval/`가 `tools`를 import하게 하지 마라. 이유: 의존 방향은 agent·mcp_server → tools → retrieval 한 방향이다(ADR-029).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
