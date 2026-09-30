# Step 3: get-document

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/retrieval/access.py`(`UserContext`·`access_filter`), `src/kodocs/retrieval/lookup.py`(문서 조회 쿼리 작성 방식), `src/kodocs/retrieval/search.py`(`ChunkHit` 확장 필드, step 2 결과), `src/kodocs/retrieval/opensearch.py`
- `src/kodocs/retrieval/index_schema.py`(`start`·`end`·`section_key`·`chunk_id`·`doc_id` 매핑), `src/kodocs/retrieval/index_build.py`(청크 문서 필드 구성)
- `tests/retrieval/`의 기존 fake client 패턴, `tests/test_integration_index_names.py`, `tests/test_m5_opensearch_integration.py`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

`retrieval/`에 **`authorized_get_document`**를 추가한다(`get_document` 도구의 유일한 데이터 경로). 다른 파일은 건드리지 않는다.

### 시그니처
```python
@dataclass(frozen=True)
class DocumentChunk:  # 필드: chunk_id, section_key, section_kind, start, end, text
    ...

@dataclass(frozen=True)
class DocumentResult:  # 필드: doc_id, title, chunks: tuple[DocumentChunk, ...]
    ...

def authorized_get_document(
    client: OpenSearchClient,
    index: str,
    ctx: UserContext,
    doc_id: str,
    *,
    section_key: str | None = None,
    size: int = 200,
) -> DocumentResult | None: ...
```
- 파일 위치: `src/kodocs/retrieval/document.py`.
- 쿼리는 `bool.filter = [access_filter(ctx), {"term": {"doc_id": doc_id}}]`(+ `section_key`가 있으면 `{"term": {"section_key": section_key}}`)이며 `start` 오름차순, 동률이면 `chunk_id` 오름차순으로 정렬한다. 접근 필터는 **같은 쿼리 안에서** 적용한다(사후 필터·별도 권한 조회 금지, ADR-010).
- 결과 청크가 0개면 `None`을 반환한다. **권한 없는 문서와 존재하지 않는 문서를 구분할 수 없어야 한다**(두 경우 모두 같은 요청 본문 구조·같은 반환 `None`). 예외 메시지·로그에 문서 존재 여부를 암시하는 문구를 넣지 않는다.
- 구조 청킹 인덱스(청크가 겹치지 않는 인덱스)를 전제로 하며 청크를 `start` 순으로 이어 붙이는 것은 도구 계층(step 4) 몫이다. 이 함수는 청크 목록만 돌려준다.
- 입력 검증: `doc_id`·`section_key`는 비어 있지 않은 문자열이어야 하고 `size`는 양의 정수여야 한다(위반은 `ValueError`, 메시지에 사용자 값 전체를 넣지 마라).
- `start`는 매핑에서 `index: false`이지만 정렬 가능한지(doc values 기본값)를 OpenSearch 3.x 매핑 문서 지식이 아니라 **실제 매핑 정의와 fake 테스트 + 통합 테스트**로 확인한다. 정렬이 막히면 `chunk_id`(적재 순서) 정렬로 대체하고 그 사유를 코드 주석 한 줄로 남긴다.

### 테스트
- fake client: 요청 본문에 `access_filter(ctx)`와 `term doc_id`가 함께 있음, 정렬 필드, `section_key` 유무에 따른 필터, 결과 0건이면 `None`, 권한 없는 문서·없는 문서에 대해 **요청 본문 구조가 동일**하고 결과가 같음.
- 통합 테스트(`@pytest.mark.integration`, 사용자 실행): 기존 통합 테스트 관례를 따르며 **uuid 임시 인덱스만** 만들고 삭제한다. 실측 인덱스 이름(`kodocs-m5-(composite|pypdf|upstage)-(fixed|structural)`)을 쓰면 `tests/test_integration_index_names.py` 가드가 실패해야 한다(가드 확장 필요 여부 확인).

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - `retrieval/`가 `tools`·`agent`·`mcp_server`·`ui`·langchain·langgraph를 import하지 않는가(기존 계층 가드 통과)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 권한 확인을 별도 조회나 Python 후처리로 하지 마라. 이유: 모든 검색은 `authorized_*`에서 쿼리 안에 pre-filter를 적용한다(ADR-010). 접근 필터 없는 doc_id 조회는 존재 여부를 노출한다.
- `None` 대신 '권한 없음' 예외·전용 반환값을 만들지 마라. 이유: 권한 없음과 미존재를 구분할 수 없어야 존재 여부가 새지 않는다(규칙 17).
- 실측 인덱스를 삭제·재생성하거나 `build_index`를 호출하는 통합 테스트를 만들지 마라. 이유: FP-046(통합 테스트가 실측 인덱스를 삭제해 M5 실측이 실패).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
