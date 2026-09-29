# Step 4: chunkers

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/chunking/corpus.py` — step 3 산출물(`CorpusDoc`, `Section`, `load_corpus`)
- `data/manifest/m5_annex_mapping.json`, `data/manifest/m5_prior_versions.json`
- `docs/ADR.md`의 ADR-028 8번(고정 청킹 크기 규칙)과 ADR-007 변경 기록

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

청커 두 종류와 고정 청크 크기 계산기를 만든다. 신규 파일: `src/kodocs/chunking/chunkers.py`, `tests/chunking/test_chunkers.py`.

### 시그니처

```python
@dataclass(frozen=True)
class Chunk:
    chunk_id: str        # f"{doc_id}#{chunking}-{seq:04d}"
    doc_id: str
    chunking: str        # "structural" | "fixed"
    seq: int
    prefix: str          # 구조 청킹의 경로 prefix(규정만, 예: "이사회 운영규정 > 제2장 > 제5조(소집)"), 없으면 ""
    body: str            # doc.text[start:end] 그대로(원문 슬라이스, 정제 없음)
    start: int           # doc.text 안 문자 범위(반열림)
    end: int
    section_kind: str | None
    section_key: str | None   # 조 키·별표 id 등

    @property
    def text(self) -> str: ...   # 색인·임베딩에 쓰는 텍스트 = prefix가 있으면 prefix + "\n" + body, 없으면 body

def chunk_structural(doc: CorpusDoc) -> list[Chunk]: ...
def chunk_fixed(doc: CorpusDoc, *, size: int, overlap: int) -> list[Chunk]: ...
def fixed_size_from_structural(chunks: Sequence[Chunk]) -> tuple[int, int]: ...   # (size, overlap)
```

### 규칙

1. **구조 청킹**: `doc.sections`를 그대로 청크로 만든다. 규정은 조 단위(경로 prefix 포함), 별표는 표 단위(별표 구간 1개 = 청크 1개 — 표가 여러 개면 별표에 붙은 표 텍스트 전체가 한 청크), 회의록은 안건 소절, 장애보고서는 섹션, 그 외 합성 유형은 `##` 섹션. 구간이 없는 텍스트(서문·구간 사이 공백)는 버리지 말고 앞 구간에 붙이지도 말고, `preamble` 구간이 있으면 청크로 만들고 아니면 청크로 만들지 않는다 — 어느 쪽인지 `corpus.py`의 `Section` 정의를 읽고 정해 테스트로 고정한다. **모든 청크의 `body`는 `doc.text[start:end]`와 정확히 같아야 한다**(테스트로 전수 검사, FP-020). 빈 청크는 만들지 않는다.
2. **고정 청킹**: `doc.text` 위를 `size`자 창으로 `size - overlap` 간격으로 미끄러뜨려 자른다. 문장·조 경계를 맞추지 않는다. 마지막 창은 끝을 문서 끝에 맞춘다(짧은 꼬리 청크가 직전 청크에 완전히 포함되면 만들지 않는다). prefix는 없다.
3. **고정 청크 크기 계산**: 조합본 코퍼스 전체(`load_corpus("composite")`: 합성 200 + 실제 현행 + 개정 전 5)의 구조 청크 길이(`len(body)`, 문자)의 **중앙값을 50자 단위로 반올림**(정확히 중간이면 올림)한 값 = `size`, `overlap = size * 20 // 100`. 이 규칙은 검색 결과를 보기 전에 확정한다. 계산은 CLI `python -m kodocs.chunking.chunkers`로 하고 결과와 근거를 **`data/manifest/m5_index.json`의 `chunking` 키**에 기록한다: `fixed_size`, `fixed_overlap`, 구조 청크 수·중앙값·최소·최대·p90, 문서 유형별·출처별 구조 청크 수, 고정 청크 총수, 코퍼스 문서 수(무엇을 세는지 필드 이름에 명시), 계산 날짜. 이 파일은 이후 step(index-build)이 다른 키를 추가하므로, **기존 키를 보존하는 읽고-병합해-쓰기** 방식으로 갱신한다.
4. CLI를 이 step에서 로컬에서 실행해 `m5_index.json`을 만들고, `docs/ADR.md`의 ADR-028 8번과 ADR-007 변경 기록에 확정 값(크기·겹침·구조 청크 중앙값)을 덧붙인다(기존 문구는 지우지 말고 "확정값" 문장을 추가한다).
5. 진행 출력(`flush=True`)과 실패 처리: 문서 하나에서 청커가 예외를 내면 doc_id와 사유를 목록에 남기고 계속하되, 실패가 1건이라도 있으면 CLI는 비정상 종료한다(자동으로 삼키지 않는다).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.chunking.chunkers
test -f data/manifest/m5_index.json
grep -q "고정 청크 크기" docs/ADR.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - `m5_index.json`의 `chunking.fixed_size`가 50의 배수이고 `fixed_overlap`이 그 20%인가?
   - 구조 청크 `body == doc.text[start:end]` 전수 테스트가 있는가?
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
- `Chunk.body`를 정제·공백 정리·prefix 합치기로 바꾸지 마라. 이유: qrels 문자 범위 매핑과 원문 동일성이 이 필드에 달려 있다(FP-020). prefix는 별도 필드이고 색인 텍스트만 합친다.
- 고정 크기를 512토큰이나 500자로 두지 마라. 이유: 사용자가 크기 규칙을 "구조 청크 길이 중앙값 반올림, 겹침 20%"로 바꿨다(ADR-028). 구조 청킹 결과를 본 뒤 검색 성능을 보고 크기를 조정하지 마라(검색 결과를 보기 전에 확정).
- 실제 데이터 없이 통과하도록 CLI를 스킵 처리하지 마라. 이유: 크기 값은 실제 코퍼스로 계산해야 한다. 코퍼스가 비어 있으면 실패로 멈춘다(FP-031).
- 청크 텍스트·조문 원문을 `m5_index.json`에 넣지 마라(통계 숫자만).
