# Step 7: index-build

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/chunking/corpus.py`, `src/kodocs/chunking/chunkers.py`
- `src/kodocs/retrieval/opensearch.py`, `src/kodocs/retrieval/index_schema.py`, `src/kodocs/retrieval/embedding.py`
- `data/manifest/m5_index.json`(step 4가 만든 `chunking` 키), `data/manifest/m5_annex_mapping.json`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

코퍼스 로드 → 청킹 → 임베딩 → 벌크 적재를 잇는 색인 빌더를 만든다. 신규 파일: `src/kodocs/retrieval/index_build.py`, `tests/retrieval/test_index_build.py`.

### 시그니처

```python
@dataclass(frozen=True)
class IndexSpec:
    condition: ParserCondition          # composite | pypdf | upstage
    chunking: Literal["structural", "fixed"]

def m5_index_specs() -> list[IndexSpec]:
    # 4개: (composite, fixed), (composite, structural), (pypdf, structural), (upstage, structural)

def build_index(spec: IndexSpec, *, client: OpenSearchClient, embedder: Embedder, root: Path = Path("."), recreate: bool = True) -> BuildReport
def main(argv: Sequence[str] | None = None) -> int     # CLI: --all | --condition --chunking, --dry-run(청크 수·예상 임베딩 수만 출력, 네트워크·OpenSearch 접속 없음), --embedder {bge-m3,fake}
```

### 규칙

1. **적재 문서**: `load_corpus(spec.condition)`의 모든 문서(합성 + 실제 + 개정 전 5건). 청커는 `chunk_structural` 또는 `chunk_fixed(size, overlap)`(값은 `m5_index.json`의 `chunking` 키에서 읽는다).
2. **색인 문서**: `text_std`·`text_nori`에 `chunk.text`(prefix 포함, 같은 텍스트), `vector`에 임베딩, 문서 메타(`access_*`, `team`, `doc_type`, `source`, `is_current`, `rule_id`, `date`), 청크 메타(`chunk_id`, `section_key`, `section_kind`, `start`, `end`), 식별자 필드(`ids_*`)는 **청크 본문 텍스트에서** 정규식으로 추출해 채우고(FP-050 — 분석하지 않는 keyword 필드), 규정 청크는 그 조의 `"{규정명} 제N조"` 정규화 값을 `ids_regulation`에 넣는다. 문서 전체의 식별자를 모든 청크에 복제하지 마라(청크 단위 정답 추적이 무의미해진다).
3. **멱등**: `recreate=True`면 인덱스를 삭제 후 다시 만든다. 파이프라인은 `ensure_pipelines`. 같은 입력으로 두 번 돌려도 같은 문서 수·같은 `_id`(=`chunk_id`)가 나온다.
4. **항목 실패 처리(FP-030)**: 임베딩·벌크 항목 하나의 실패는 실패 목록(`chunk_id`, 사유)에 남기고 계속한다. 실패가 1건이라도 있으면 종료 코드는 비정상이며 manifest에 실패 수를 기록한다. 색인된 청크가 0건이면 실패(FP-031). 색인 후 `_count`가 적재 대상 청크 수와 다르면 실패.
5. **manifest**: `data/manifest/m5_index.json`에 `indexes` 키를 추가한다(기존 `chunking` 키 보존, 인덱스 이름을 키로 하는 읽고-병합해-쓰기, FP-032). 각 인덱스: 조건, 청킹, 문서 수(합성/실제 현행/개정 전), 청크 수, 실패 수, 임베더 `model_id`·`revision`·설정 해시·장치·`init_s`, 별표 fallback 수, 빌드 시각, 코드 커밋 해시(`git rev-parse HEAD`, 실패하면 `null`), 색인 필드 스키마 해시. 원문 텍스트 금지.
6. **진행 출력**: 청킹·임베딩·벌크 각 단계에서 N건마다 `flush=True`로 진행 줄 출력(FP-034). 진행 줄에 텍스트·URL 자격 정보 금지.
7. 테스트: `FakeEmbedder` + `httpx.MockTransport`로 (a) 색인 문서 필드·`_id`·식별자 keyword 채움, (b) 재실행 멱등, (c) 항목 실패 시 계속 진행·비정상 종료, (d) 0건이면 실패, (e) `m5_index_specs()`가 4개이며 조건·청킹 조합이 위와 같음, (f) `--dry-run`이 OpenSearch를 호출하지 않음. 통합 테스트(`integration`)로 작은 fixture 코퍼스를 실제 OpenSearch에 색인해 `_count`를 확인하는 케이스 1개를 `tests/test_m5_opensearch_integration.py`에 추가한다.
8. 실제 BGE-M3 임베딩·`make up`·전체 색인은 이 step에서 실행하지 마라(사용자가 나중에 실행). `--embedder fake` 경로만 로컬에서 시험해도 된다.

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
   - 인덱스 4개(조합본 고정·구조, pypdf 구조, upstage 구조) 정의가 ADR-028 5번과 같은가?
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
- 식별자를 분석 필드에서만 찾게 만들지 마라. 이유: 분석기가 하이픈에서 쪼갠다(FP-050, ADR-017). `ids_*` keyword 필드에 청크 본문에서 추출해 넣는다.
- 색인 중 항목 하나가 실패했다고 전체를 중단하거나, 반대로 실패를 삼키고 성공으로 끝내지 마라. 이유: 배치의 항목 실패는 기록하고 계속하되(FP-030) 0건 성공을 성공으로 끝내면 안 된다(FP-031).
- `m5_index.json`을 덮어써 `chunking` 키를 지우지 마라. 이유: 고정 청크 크기 확정값이 이 파일에 있다. append-only 성격의 기록은 최신 키 기준으로 읽는다(FP-032).
- BGE-M3 모델 로드·다운로드, Docker 기동, 실제 색인 실행을 하지 마라. 이유: 승인·사용자 실행 대상이다(사용자 실행 분담).
- 청크 텍스트를 manifest·로그에 남기지 마라.
