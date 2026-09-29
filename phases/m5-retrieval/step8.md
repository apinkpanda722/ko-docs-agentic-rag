# Step 8: authorized-search

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/retrieval/opensearch.py`, `src/kodocs/retrieval/index_schema.py`, `src/kodocs/retrieval/embedding.py` — 앞 step 산출물(필드 이름 고정)
- `src/kodocs/eval/golden.py` — `can_read(asker, access)` (M4에서 확정한 권한 정책의 기준 구현), `Asker`
- `docs/ADR.md` ADR-010(pre-filter, 단일 인가 지점)·ADR-019(간접 인젝션은 검색 전 인가로 막음)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

권한 pre-filter가 적용된 검색 함수를 만든다. 신규 파일: `src/kodocs/retrieval/access.py`, `src/kodocs/retrieval/search.py`, `tests/retrieval/test_access.py`, `tests/retrieval/test_search.py`, 통합 테스트는 `tests/test_m5_opensearch_integration.py`에 추가(`integration`).

### 시그니처

```python
@dataclass(frozen=True)
class UserContext:
    team: str
    grade: int

def access_filter(ctx: UserContext) -> dict: ...      # OpenSearch bool.filter 절
def can_read_doc(ctx: UserContext, *, level: str, teams: Sequence[str], min_grade: int) -> bool: ...   # 순수 파이썬 기준 구현

@dataclass(frozen=True)
class SearchParams:
    method: Literal["bm25", "knn", "hybrid_rrf", "hybrid_weighted"]
    analyzer: Literal["standard", "nori"] = "nori"   # bm25·hybrid의 텍스트 필드 선택. knn은 무시
    candidate_chunks: int = 100                      # 하위 쿼리별 후보 수 = 최종 청크 목록 길이
    bm25_weight: float | None = None                 # hybrid_weighted 전용 (0<w<1)
    rank_constant: int = 60                          # hybrid_rrf

@dataclass(frozen=True)
class ChunkHit:
    chunk_id: str; doc_id: str; score: float; start: int; end: int
    section_key: str | None; section_kind: str | None

@dataclass(frozen=True)
class DocHit:
    doc_id: str; score: float; best_chunk_id: str        # 문서 점수 = 소속 청크 최고 점수

@dataclass(frozen=True)
class SearchResult:
    chunks: tuple[ChunkHit, ...]      # 점수 내림차순, 길이 ≤ candidate_chunks
    docs: tuple[DocHit, ...]          # 문서별 max 점수 내림차순(동점은 doc_id 오름차순) — 순위 전체
    embed_ms: float | None
    search_ms: float

def authorized_search(client: OpenSearchClient, index: str, ctx: UserContext, query: str,
                      params: SearchParams, embedder: Embedder | None = None) -> SearchResult: ...
```

### 핵심 규칙 (CRITICAL)

1. **권한 규칙은 M4 `can_read`와 정확히 같다**: `public`은 모두 허용. 그 외는 `ctx.team ∈ teams`여야 하고, `team` 수준은 그것으로 허용, `restricted`는 추가로 `ctx.grade >= min_grade`. 그 밖의 `level`은 거부. 실제 규정(전부 `public`)도 같은 경로를 탄다. `access_filter`는 이 규칙을 OpenSearch 절로 옮긴 것이다(`access_level`, `access_teams`, `access_min_grade` 필드 사용). 테스트는 `kodocs.eval.golden.can_read`를 기준 구현으로 두고 `can_read_doc`·`access_filter` 해석기(작은 절 평가기)가 모든 조합(level 3 × 팀 일치/불일치 × 직급 미만/이상)에서 같은 결과를 내는지 교차 검증한다. `retrieval` 패키지가 `kodocs.eval`을 import하지는 않는다(테스트만 import).
2. **pre-filter**: 권한 필터를 **모든 하위 쿼리 안에** 넣는다 — BM25 `bool.filter`, kNN 쿼리의 `filter`(효율적 사전 필터), hybrid의 각 하위 쿼리. 검색 결과를 받은 뒤 걸러내는 후처리 필터를 절대 두지 않는다. 쿼리 본문 생성 함수를 순수 함수로 분리하고, 테스트에서 **본문 안의 모든 검색 하위 쿼리에 접근 필터가 들어 있는지 재귀적으로 확인하는 도우미**(`assert_every_subquery_prefiltered(body)`)로 4개 방식 모두 검사한다.
3. **사용자 컨텍스트는 `UserContext` 인자로만 받는다**(질의 문자열·도구 인자에서 파싱하지 않는다). 함수 시그니처에 권한 값을 문자열 인자로 열어 두지 마라.
4. 방식별 쿼리: `bm25` = `text_std` 또는 `text_nori`에 `match`; `knn` = `vector` kNN(k = candidate_chunks); `hybrid_rrf` = `hybrid` 쿼리(BM25 하위 + kNN 하위) + 파이프라인 `kodocs-m5-rrf`(rank_constant=60); `hybrid_weighted` = 같은 하위 쿼리 + 가중치별 파이프라인(`weighted_pipeline_body`가 만드는 이름 규칙 그대로). 질의 임베딩은 `embedder.embed([query], kind="query")`. `knn`·hybrid인데 embedder가 없으면 `ValueError`.
5. **문서 묶음**: 청크 목록에서 문서별 최고 점수로 문서 순위를 만든다. 동점은 `doc_id` 오름차순(결정론). 청크 순서도 (점수 내림차순, chunk_id 오름차순)으로 결정론.
6. 권한 없는 문서는 결과 어디에도(청크·문서·오류 메시지·카운트) 나타나지 않는다. 결과가 0건이어도 "권한이 없어서"임을 드러내는 메시지를 만들지 않는다(존재 여부 비노출, 아키텍처 MCP 계약).
7. **통합 테스트**(`integration`): 작은 인덱스에 제한 문서(다른 팀 전용, 높은 유사도로 설계)를 넣고 4개 방식 모두에서 다른 팀 사용자·낮은 직급 사용자에게 그 문서가 청크·문서 어디에도 안 나오는지, 권한 있는 사용자에게는 나오는지 확인한다. kNN 하위 쿼리에서도 필터가 후처리가 아닌 사전 필터로 동작하는지(제한 문서가 상위 k를 채워 결과가 비지 않는지 — 제한 문서를 k보다 많이 넣어 허용 문서가 여전히 반환되는지)를 포함한다.

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
   - 모든 하위 쿼리에 접근 필터가 들어 있음을 재귀 검사하는 테스트가 4개 방식 모두에 있는가?
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
- 검색 후 결과에서 권한 없는 문서를 걸러내는 방식을 쓰지 마라. 이유: post-filter는 kNN 후보를 제한 문서가 채우면 결과가 비거나 누설 위험이 생긴다(ADR-010, AGENTS.md CRITICAL: 검색 전에 적용).
- 권한 규칙을 별도로 재해석하지 마라(예: `team` 수준에 `min_grade`를 적용). 이유: M4 qrels·권한 문항이 `can_read`를 기준으로 확정됐다. 규칙이 어긋나면 평가 전제 검사(step 11)가 깨진다. 교차 검증 테스트로 강제한다.
- `retrieval`에서 `kodocs.eval`·`kodocs.agent`·`langchain`을 import하지 마라(계층 가드, step 1).
- 임의의 사용자 컨텍스트를 질의 문자열에서 뽑거나 함수 기본값으로 관대하게 두지 마라(예: 컨텍스트가 없으면 전체 공개). 컨텍스트 누락은 오류다.
- 권한 관련 오류 메시지에 문서 ID·제목·존재 여부를 넣지 마라.
