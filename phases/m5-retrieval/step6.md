# Step 6: embedder

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/retrieval/index_schema.py` — 벡터 차원(1024)·필드 이름
- `src/kodocs/parsing/docling_adapter.py` — 지연 import·`prepare()`·초기화 시간 분리 패턴 참고
- `docs/ADR.md` ADR-006(BGE-M3 로컬)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

임베더 프로토콜, BGE-M3 어댑터(지연 import), 디스크 캐시, fake 임베더를 만든다. 신규 파일: `src/kodocs/retrieval/embedding.py`, `tests/retrieval/test_embedding.py`.

### 시그니처

```python
class Embedder(Protocol):
    model_id: str
    revision: str
    dim: int
    settings: Mapping[str, object]      # max_seq_length, normalize, device 등 — 설정 해시 산출에 쓴다
    def count_tokens(self, texts: Sequence[str]) -> list[int]: ...   # 모델 토크나이저로 잰 토큰 수(특수 토큰 포함). 임베딩 잘림 검사용, 모델 로딩 없이 토크나이저만 필요하면 그렇게
    def prepare(self) -> float: ...     # 모델 로딩 등 1회성 초기화. 걸린 초 반환(init_s)
    def embed(self, texts: Sequence[str], *, kind: Literal["passage", "query"]) -> list[list[float]]: ...

class FakeEmbedder: ...                 # 텍스트 해시 기반 결정론 벡터(차원 인자), 외부 의존 없음. 유사 텍스트끼리 코사인이 높도록 문자 n-gram 해시 버킷 방식으로

class Bge3Embedder:                     # BAAI/bge-m3, dense 벡터만, L2 정규화, max_seq_length는 모델 지원 최대값(아래 규칙 8)
    def __init__(self, *, model_path_or_id: str = "BAAI/bge-m3", device: str | None = None, batch_size: int = 16,
                 max_batch_tokens: int = 16384, max_seq_length: int | None = None, local_files_only: bool = True): ...

class CachedEmbedder:                   # 디스크 캐시 래퍼: 키 = sha256(model_id | revision | max_seq_length | normalize | kind | text)
    def __init__(self, inner: Embedder, cache_dir: Path): ...

def embedder_settings_hash(e: Embedder) -> str: ...     # 12자리, parsing.schema.settings_hash와 같은 방식
```

### 규칙

1. **지연 import**: `sentence_transformers`·`torch`는 `Bge3Embedder.prepare()` 안에서만 import한다. 모듈 import만으로 무거운 패키지가 로드되거나 ImportError가 나면 안 된다(`make verify`는 `embed` 의존성 없이 통과해야 한다). 패키지가 없으면 `prepare()`가 "`uv sync --group embed` 필요"라는 명확한 오류를 낸다.
2. **`local_files_only=True` 기본**: 모델 다운로드는 이 코드가 하지 않는다(사용자가 별도로 받는다, `make embed-models`는 step 15에서 만들고 사용자가 실행). 다운로드가 필요한 상황이면 오류로 멈춘다.
3. **1회성 초기화 분리(FP-036)**: 모델 로딩 시간은 `prepare()`가 반환하는 `init_s`로만 기록하고 문서·청크별 시간에 섞지 않는다. 실행 장치(mps·cuda·cpu)와 `revision`(로컬 모델 스냅샷의 커밋 해시 — 경로에서 읽되 못 읽으면 `"unknown"` 대신 오류)을 `settings`에 넣어 기록한다(FP-044).
4. **캐시 키는 벡터를 결정하는 모든 것을 포함(FP-033)**: 위 키 정의를 그대로 쓴다. 캐시 저장은 원자적으로(임시 파일 → 이름 변경). 캐시 디렉토리는 `data/interim/embeddings/`(비커밋)이며 벡터 파일만 담는다. 캐시 히트·미스 수를 반환하는 `stats`를 제공한다.
5. **배치 진행 출력(FP-034)**: `CachedEmbedder.embed`는 N건마다 진행 줄을 `flush=True`로 출력한다(텍스트 내용은 출력하지 않는다).
6. **쿼리 접두어 없음**: BGE-M3는 지시문 접두어가 필요 없다. `kind`는 캐시 키·로깅 용도로만 구분한다.
8. **max_seq_length 명시 설정(잘림 방지, 사용자 확정)**: `sentence-transformers`의 기본값(모델 설정에 의존)에 맡기지 않는다. 다운로드된 로컬 스냅샷의 `sentence_bert_config.json`과 `config.json`(`max_position_embeddings`)에서 모델이 지원하는 최대 길이를 읽어(예상 8192, 확인된 값: `sentence_bert_config.json`의 `max_seq_length: 8192`, `max_position_embeddings: 8194`) `model.max_seq_length`에 **명시적으로 설정**하고 `settings["max_seq_length"]`에 기록한다. 생성자 인자 `max_seq_length`가 주어지면 그 값을 쓰되 모델 지원 최대값을 넘으면 오류다. 설정 파일에서 값을 읽지 못하면 조용히 기본값으로 떨어지지 말고 오류로 멈춘다. 이 값은 캐시 키와 `embedder_settings_hash`에 포함된다(step 7이 `m5_index.json`에 기록).
9. **`count_tokens`**: 같은 토크나이저로 텍스트별 토큰 수를 잰다(잘림 판정은 `토큰 수 > max_seq_length`). 모델 가중치 로딩 없이 토크나이저만으로 동작하게 하되(가능하면), 그렇지 않으면 `prepare()` 이후에만 호출 가능하다고 docstring에 적는다. `FakeEmbedder.count_tokens`는 결정론적 근사(예: 문자 수 // 2 + 2)로 두어 테스트가 한도 초과 경로를 만들 수 있게 한다(생성자에서 `max_seq_length` 인자 지원).
10. **긴 청크 배치(메모리 초과 방지)**: `Bge3Embedder.embed`는 입력을 **토큰 길이순으로 정렬**해(내림차순) 배치로 묶고, 배치당 (최대 토큰 수 × 배치 크기)가 `max_batch_tokens`를 넘지 않게 배치 크기를 줄인다. 한 청크가 혼자 한도를 넘어도 단독 배치로 처리한다. **결과는 반드시 입력 순서대로 반환**한다(정렬은 내부 구현). `CachedEmbedder`는 캐시 미스만 모아 inner에 넘긴다.
11. 테스트는 `FakeEmbedder`와 가짜 inner로: 캐시 히트가 inner를 다시 부르지 않음, 키에 모델·리비전·kind가 반영됨(다르면 미스), 원자적 쓰기, `Bge3Embedder` 모듈 import가 `sentence_transformers` 없이 성공하고 `prepare()`는 명확한 오류를 냄(monkeypatch로 ImportError 모사), 정규화 벡터 길이 1, 길이순 배치가 입력 순서를 보존함(길이가 뒤섞인 입력), `max_batch_tokens` 초과 시 배치가 나뉨, `max_seq_length`를 스냅샷 설정 파일(테스트용 임시 디렉토리의 가짜 `sentence_bert_config.json`)에서 읽고 파일이 없으면 오류, 모델 최대값 초과 인자는 오류, `count_tokens`가 한도 초과 판정에 쓰일 수 있음.

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
   - `import kodocs.retrieval.embedding`이 sentence_transformers 없이 되는가?
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
- `pyproject.toml`·`uv.lock`을 수정하거나 `sentence-transformers`·`torch`를 설치하지 마라. 이유: 새 의존성·대용량 설치는 사용자 승인 후 사용자가 설치한다(FP-040). 어댑터는 지연 import로 작성한다.
- 모델을 다운로드하지 마라(`local_files_only=True` 유지). 이유: 다운로드는 승인 대상이다.
- 모델 로딩 시간을 청크별 임베딩 시간에 섞지 마라. 이유: 1회성 초기화가 처리 시간을 부풀린 실패다(FP-036).
- `max_seq_length`를 1024 등 임의 값으로 낮추거나 `sentence-transformers` 기본값에 맡기지 마라. 이유: 값이 조용히 청크를 잘라 정답 청크의 임베딩을 훼손한다(사용자 확정: 모델 지원 최대값을 명시 설정하고 기록). 큰 청크의 메모리는 길이순 배치로 해결한다.
- 캐시 키에서 모델 리비전·정규화 여부·max_seq_length를 빼지 마라. 이유: 멱등성 키가 출력을 결정하는 값과 다르면 서로 다른 벡터가 같은 캐시를 쓴다(FP-033).
