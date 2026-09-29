# Step 5: index-schema

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/chunking/chunkers.py` — `Chunk`
- `infra/Dockerfile`, `infra/docker-compose.yml`, `tests/test_opensearch_integration.py` — 로컬 OpenSearch 3.8.0(nori 플러그인, 보안 끔, 127.0.0.1:9200)
- `docs/ADR.md` ADR-005(hybrid·RRF·정규화 search pipeline)·ADR-017(식별자 keyword)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

OpenSearch 인덱스 정의·search pipeline·얇은 HTTP 클라이언트를 만든다. 신규 파일: `src/kodocs/retrieval/opensearch.py`(httpx 기반 클라이언트), `src/kodocs/retrieval/index_schema.py`, `tests/retrieval/test_index_schema.py`(`tests/retrieval/__init__.py` 필요 시), 통합 테스트 `tests/test_m5_opensearch_integration.py`(`@pytest.mark.integration`).

### 시그니처

```python
class OpenSearchClient:                         # httpx.Client 주입 가능(테스트는 httpx.MockTransport)
    def __init__(self, base_url: str = "http://127.0.0.1:9200", *, client: httpx.Client | None = None): ...
    def request(self, method: str, path: str, json: object | None = None) -> dict: ...   # 4xx/5xx는 OpenSearchError(status, body 요약)
    def bulk(self, index: str, docs: Iterable[tuple[str, dict]]) -> BulkResult: ...      # NDJSON, 항목별 오류 수집

INDEX_PREFIX = "kodocs-m5"
def index_name(condition: str, chunking: str) -> str: ...      # "kodocs-m5-composite-structural" 등
def index_body(*, dim: int = 1024) -> dict: ...                # settings + mappings
def rrf_pipeline_body(rank_constant: int = 60) -> dict: ...
def weighted_pipeline_body(bm25_weight: float) -> dict: ...    # min_max 정규화 + arithmetic_mean 가중(BM25, kNN 순)
def ensure_pipelines(client) -> None: ...
```

### 인덱스 매핑(필드 이름을 이 이름으로 고정, 이후 step이 의존)

- `settings`: `index.knn: true`, 샤드 1·레플리카 0, 분석기 `nori_analyzer`(tokenizer `nori_tokenizer`, `decompound_mode: mixed`, 필터 `nori_readingform`·소문자화. `nori_part_of_speech` 품사 제거 필터는 기본 stoptags를 써서 넣는다).
- `text_std`(text, `standard` 분석기), `text_nori`(text, `nori_analyzer`), `vector`(`knn_vector`, dim 1024, HNSW, 엔진은 효율적 필터링을 지원하는 것 — `lucene` 또는 `faiss` 중 하나를 고르고 이유를 코드 주석 한 줄로 남긴다, 공간 `innerproduct` 또는 `cosinesimil`: 임베딩을 정규화하므로 일관되게 하나만).
- 식별자·메타(모두 분석하지 않는 `keyword`): `chunk_id`, `doc_id`, `chunking`, `doc_type`, `source`, `team`, `rule_id`, `section_key`, `section_kind`, `ids_ticket`, `ids_approval`, `ids_incident`, `ids_service`, `ids_regulation`(모두 배열), `access_level`, `access_teams`(배열); `access_min_grade`(integer), `is_current`(boolean), `date`(keyword 또는 date), `start`·`end`(integer, `index: false` 허용), `title`(text 아님 — `keyword`, `index: false`). 문서 단위 필드는 청크마다 복제한다.
- 청킹·파서 조건 정보는 인덱스 이름과 `_meta`에 기록한다(`_meta`: condition, chunking, embedder 설정 해시 자리).
- **hybrid search pipeline**: RRF는 `score-ranker-processor`(`combination.technique: rrf`, `rank_constant: 60`), 가중합은 `normalization-processor`(`normalization.technique: min_max`, `combination.technique: arithmetic_mean`, `parameters.weights: [bm25_weight, 1 - bm25_weight]`). 파이프라인 이름은 `kodocs-m5-rrf`, `kodocs-m5-weighted-w{int(round(w*100)):03d}`처럼 결정론적으로 만든다(가중치별 파이프라인을 필요한 만큼 생성).

### 검증

- 단위 테스트: `httpx.MockTransport`로 인덱스 생성·파이프라인 생성·bulk 요청 본문을 검증한다(필드 이름·타입·`keyword` 식별자 필드·nori 분석기 정의·RRF `rank_constant == 60`·가중합 `weights` 합 1·정규화 `min_max`). 벌크 NDJSON 형식(액션 줄 + 문서 줄, 끝 개행), 항목별 오류 수집.
- 통합 테스트(`integration`): 실행 중 OpenSearch가 있을 때만. 작은 인덱스를 만들고 (1) `PAY-1234`와 `PAY-1233`이 standard·nori 필드에서는 쪼개져 섞이지만 `keyword` 필드 `term` 조회는 정확히 하나만 반환하는지(FP-050을 테스트로 승격), (2) `hybrid` 쿼리에 RRF·가중합 파이프라인이 실제로 응답하는지, (3) 임시 인덱스를 삭제하는지 확인한다. **실제 응답 필드는 추측하지 말고 응답 1건을 확인해 fixture에 반영한다**(FP-011). `make verify`에서는 제외된다(마커).

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
   - 필드 이름이 위 표와 정확히 같은가(index-build·search가 이 이름에 의존한다)?
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
- `PAY-1234` 같은 식별자를 `text` 분석 필드에만 넣지 마라. 이유: 분석기가 하이픈에서 쪼개 다른 번호가 섞인다(ADR-017, FP-050). 식별자 전용 `keyword` 필드를 둔다.
- OpenSearch 응답 필드·검색 파이프라인 프로세서 이름을 기억으로 확정하지 마라. 이유: 외부 시스템의 응답·설정 스키마를 추측한 실패다(FP-011). 통합 테스트로 확인한 값을 쓰고, 확인이 불가능한 환경이면 그 사실을 `summary`에 적는다.
- `opensearch-py`나 새 의존성을 추가하지 마라. 이유: 설치는 승인 대상이다(FP-040). 기존 `httpx`만 쓴다.
- 요청·응답 로그에 텍스트 원문 전체를 남기지 마라(오류 메시지에는 상태·본문 앞 200자까지만).
