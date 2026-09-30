# Step 9: authorized-lookup-sql

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/retrieval/access.py`, `src/kodocs/retrieval/search.py` — step 8 산출물
- `src/kodocs/chunking/corpus.py` — `normalize_regulation_ref`, 식별자 추출
- `src/kodocs/synth/identifiers.py` — 식별자 정규식·`service_pattern`(이 모듈만 import한다)
- `src/kodocs/synth/db.py` — 원장 SQLite 스키마(tickets·approvals·행 단위 `access_level`·`access_teams`(쉼표 문자열)·`access_min_grade`)
- `src/kodocs/eval/golden_specs.py`의 `_row_readable`·`_run_sql` — SQL 문항 기대값이 계산되는 방식(질문자가 볼 수 있는 행만)
- `data/manifest/synth_db.json` — 원장 해시·행 수

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

`authorized_lookup_id`와 `authorized_run_sql`을 만든다. 신규 파일: `src/kodocs/retrieval/lookup.py`, `src/kodocs/retrieval/sql.py`, `tests/retrieval/test_lookup.py`, `tests/retrieval/test_sql.py`, 통합 테스트 추가(`tests/test_m5_opensearch_integration.py`).

### 1. `authorized_lookup_id` (식별자 keyword 정확 일치)

```python
IdKind = Literal["ticket", "approval", "incident", "service", "regulation"]

def extract_query_identifiers(question: str, *, service_names: Sequence[str] = ()) -> list[tuple[IdKind, str]]:
    # extract_identifiers(질문) + service_pattern(service_names). 규정 참조는 normalize_regulation_ref로 정규화한 값을 반환

def authorized_lookup_id(client, index: str, ctx: UserContext, kind: IdKind, value: str, *,
                         rank_query: str | None = None, size: int = 50) -> tuple[DocHit, ...]: ...
```

- `ids_{kind}` keyword 필드에 `term` 쿼리로 정확 일치 조회한다. **접근 필터를 같은 쿼리의 `bool.filter`에 넣는다**(step 8과 같은 `access_filter`).
- 결과 순서: `rank_query`가 있으면 `text_nori` `match`(should)로 BM25 점수를 매겨 점수 내림차순, 없으면 (`date` 내림차순, `doc_id` 오름차순). 이유: 하나의 식별자가 여러 문서에 나오므로 "1위" 판정에는 결정론적 순서가 필요하다. 어느 경우든 결정론이어야 한다(동점은 `doc_id` 오름차순).
- 청크가 아니라 문서로 묶어 반환한다(문서 점수 = 청크 최고 점수).
- 값이 없으면(오탈자·권한 밖) 빈 튜플. 권한 밖과 존재하지 않음을 구별할 수 없어야 한다.
- 식별자 값은 색인 때와 **같은 정규화 함수**를 거친다(이 step에서 새 정규화 규칙을 만들지 말고 `normalize_regulation_ref`를 재사용). `PAY-1234`와 `PAY-1233`이 섞이지 않음을 통합 테스트로 확인한다(FP-050).

### 2. `authorized_run_sql` (읽기 전용 SQL)

```python
ALLOWED_TABLES = ("tickets", "approvals")
DEFAULT_LIMIT = 100

class SqlRejected(ValueError): ...      # 거부 사유 코드만(예: not_select, table_not_allowed, multi_statement, forbidden_function)

def authorized_run_sql(db_path: Path, ctx: UserContext, sql: str, *, limit: int = DEFAULT_LIMIT) -> SqlResult: ...
# SqlResult: columns: tuple[str, ...], rows: tuple[tuple, ...], truncated: bool
```

CRITICAL 요구사항(테스트로 각각 증명):
1. **SELECT만**: 단일 문장. `;`로 이어진 다중 문장, `INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA/VACUUM`, `WITH ... DELETE` 등은 거부한다. 문자열·주석 안의 키워드 때문에 오탐/미탐이 없도록 문자열 검사가 아니라 **sqlite3 authorizer 콜백**으로 강제한다(허용 액션: `SQLITE_SELECT`, `SQLITE_READ`(허용 테이블·뷰만), `SQLITE_FUNCTION`(허용 함수 목록)).
2. **읽기 전용 연결**: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. 쓰기 시도는 연결 수준에서도 실패해야 한다.
3. **허용 테이블 목록**: `tickets`, `approvals`만. `people`·`services`·`incidents`·`teams`·`sqlite_master` 등 그 외는 거부.
4. **LIMIT 강제**: 사용자가 준 `LIMIT`이 없거나 `limit`보다 크면 `limit`으로 제한한다(쿼리를 서브쿼리로 감싸 `LIMIT`을 바깥에서 적용하는 방식 권장). `truncated`로 잘림 여부를 알린다.
5. **행 단위 권한 필터(검색 전 적용)**: 사용자에게 보이는 행만 집계에 들어가야 한다. `tickets`·`approvals`의 `access_level`·`access_teams`(쉼표 문자열)·`access_min_grade`에 **M4 `can_read` 규칙과 같은 정책**을 적용한 **임시 뷰**를 `temp` 스키마에 만들어 원본 테이블 대신 읽게 하고, authorizer는 임시 뷰를 통한 원본 읽기만 허용한다(authorizer의 4번째 인자로 뷰 안의 접근임을 식별). 뷰 정의에 들어가는 사용자 값(`team`, `grade`)은 SQL 문자열 결합이 아니라 바인딩·정수 검증으로 넣는다(SQL 인젝션 방지). 테스트: 다른 팀의 `restricted` 행이 `COUNT(*)`·`SUM(amount_krw)` 집계에 포함되지 않음을, 같은 SQL을 권한이 다른 두 사용자로 실행해 결과가 다름을 확인한다.
6. 사용자 컨텍스트는 인자 `ctx`로만 받는다. SQL 안에서 권한 컬럼을 우회하려는 시도(`SELECT ... FROM main.tickets`, `UNION`으로 원본 접근, 서브쿼리, 뷰 이름 섀도잉)를 거부하거나 필터된 행만 보이게 하는 테스트를 만든다.
7. 오류 메시지에는 거부 사유 코드만 담는다(스키마·행 내용 노출 금지).

로컬 `data/synthetic/ledger.sqlite`가 있어도 단위 테스트는 임시 SQLite fixture로 한다(원장 파일 의존 금지). 원장이 있을 때만 도는 검증은 만들지 않는다.

## Acceptance Criteria

```bash
make verify
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - `authorized_run_sql`이 뷰 섀도잉·UNION·다중 문장·PRAGMA·ATTACH를 거부하는 테스트가 있는가?
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
- SQL 안전성을 정규식·키워드 문자열 검사만으로 구현하지 마라. 이유: 문자열·주석·대소문자·인용으로 우회된다. authorizer + 읽기 전용 연결 + 허용 테이블 + LIMIT을 함께 쓴다(AGENTS.md CRITICAL).
- `run_sql`의 권한 필터를 사후에 걸러내는 방식으로 만들지 마라. 이유: 집계(`COUNT`·`SUM`) 결과에 제한 행이 이미 섞인다. 필터된 뷰를 통해서만 읽게 한다(pre-filter, ADR-010).
- 사용자 값(team·grade)을 SQL 문자열에 직접 이어 붙이지 마라. 이유: SQL 인젝션. 
- 식별자 정규화를 색인 쪽과 다르게 새로 만들지 마라. 이유: 색인·질의 정규화가 어긋나면 정확 일치가 조용히 실패한다.
- 권한 밖 행·문서가 존재한다는 사실을 오류 메시지·빈 결과 차이로 드러내지 마라.
