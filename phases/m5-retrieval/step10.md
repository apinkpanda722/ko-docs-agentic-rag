# Step 10: metrics-stats

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `docs/ADR.md` ADR-028 4·9번(지표 정의, 채택 기준), `docs/results/hypotheses.md` 공통 판정 기준
- `src/kodocs/eval/parsing_score.py` 등 기존 eval 모듈의 코드 스타일(순수 함수·dataclass)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

검색 지표와 부트스트랩을 구현한다(순수 파이썬, 새 의존성 금지 — numpy도 없다). 신규 파일: `src/kodocs/eval/m5_metrics.py`, `src/kodocs/eval/m5_stats.py`, `tests/eval/test_m5_metrics.py`, `tests/eval/test_m5_stats.py`.

### `m5_metrics.py`

```python
def doc_relevance(qrels: Sequence[tuple[str, int]]) -> dict[str, int]:   # (doc_id, rel) → doc_id별 최대 관련도. rel ∈ {0,1,2}
def recall_at_k(ranked_doc_ids: Sequence[str], rel_by_doc: Mapping[str, int], k: int = 10) -> float
    # 정답 = rel ≥ 1인 문서. 분모 = rel ≥ 1 문서 수. 분모 0이면 ValueError(호출자가 문항을 미리 걸러야 한다)
def ndcg_at_k(ranked_doc_ids, rel_by_doc, k: int = 10) -> float
    # 이득 = rel 그대로(선형 0/1/2), 할인 1/log2(순위+1), 이상적 순서는 rel 내림차순 전체 문서. 분모 0이면 ValueError
def top1_hit(ranked_doc_ids, rel_by_doc) -> bool                         # 1위 문서의 rel ≥ 1
@dataclass(frozen=True) class Span: doc_id: str; start: int; end: int
def span_hit_at_k(retrieved: Sequence[Span], answer_spans: Sequence[Span], k: int = 10) -> bool
    # 상위 k 청크 중 하나라도 같은 doc_id의 정답 구간과 반열림 구간이 1자 이상 겹치면 True
```

### `m5_stats.py`

```python
@dataclass(frozen=True)
class BootstrapResult: n: int; mean_diff: float; ci_low: float; ci_high: float; n_boot: int; seed: int

def paired_bootstrap(a: Sequence[float], b: Sequence[float], *, n_boot: int = 10_000, seed: int = 20260930) -> BootstrapResult
    # 문항별 차이 d_i = a_i - b_i 를 복원추출해 평균의 2.5·97.5 백분위 구간(백분위 방식). a, b 길이 다르면 ValueError
def independent_bootstrap(x: Sequence[float], y: Sequence[float], *, n_boot: int = 10_000, seed: int = 20260930) -> BootstrapResult
    # 각 그룹을 따로 복원추출해 mean(x) - mean(y)의 구간. H7·코퍼스 v2 규칙에 쓴다. 한 그룹이 비면 ValueError
def describe(values: Sequence[float]) -> dict         # n, mean, sd(표본, n<2면 None), median, min, max
def verdict(result: BootstrapResult, *, direction: Literal["positive", "negative"] = "positive") -> str
    # "채택"(CI가 가설 방향으로 0을 배제) | "기각(반대 방향)"(반대 방향으로 0을 배제) | "판정 보류(차이 입증 못 함)"(0 포함)
```

- 부트스트랩은 `random.Random(seed)` 하나만 쓰고 결정론이어야 한다(같은 입력·시드 → 같은 구간). 시드 기본값은 20260930(결과에 기록).
- 백분위 위치 계산 규칙을 하나로 고정해 테스트로 못박는다(정렬된 n_boot 값의 `floor(0.025*(n_boot-1))`, `ceil(0.975*(n_boot-1))` 인덱스 등 — 선택한 규칙을 docstring에 적는다).

### 테스트 (손계산 가능한 값으로)

- `recall_at_k`/`ndcg_at_k`: 관련도 2 문서 1개·1 문서 1개, 순위가 완벽/뒤바뀜/일부 누락인 경우. 이상적 DCG 손계산값과 일치. 같은 doc_id가 qrels에 rel=1, rel=2로 두 번 있으면 최대값(2)만 쓰는지(`doc_relevance`).
- 청크가 아닌 **문서** 순위를 입력으로 받으므로, 같은 문서가 순위에 두 번 나오는 입력을 거부하거나(ValueError) 함수 docstring으로 호출자 책임임을 명시하고 테스트한다 — 어느 쪽인지 하나로 정한다.
- `span_hit_at_k`: 경계가 맞닿기만 한 경우(겹침 아님), 1자 겹침, 다른 doc_id.
- 부트스트랩: 두 벡터가 같으면 구간이 [0,0]이고 `verdict`는 "판정 보류", 항상 a>b이면 구간 하한 > 0이라 "채택", 반대 방향 확정이면 "기각(반대 방향)". 결정론(같은 시드 두 번 같은 결과). `independent_bootstrap`은 그룹 크기가 달라도 동작. 표본이 매우 작은(n=1) 경우 동작 규칙(구간이 점 하나)을 명시한다.

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
   - 시드·백분위 규칙이 docstring과 테스트에 고정돼 있는가?
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
- NDCG 이득을 `2**rel - 1`로 바꾸지 마라. 이유: 사전 등록된 정의는 관련도 0/1/2 선형 이득이다(ADR-028 4번). 결과를 본 뒤 정의를 바꾸지 않는다.
- Recall·NDCG의 분모가 0인 문항을 조용히 0점(또는 1점)으로 처리하지 마라. 이유: 정답이 없는 문항이 분모에 들어가 지표를 왜곡한 실패다(FP-021). 호출자가 대상 문항을 미리 걸러야 하며 함수는 오류를 낸다.
- numpy·scipy 등 새 의존성을 추가하지 마라. 이유: 설치는 승인 대상이다(FP-040). 표준 라이브러리 `random`으로 충분하다.
- `verdict`에서 "CI가 0을 포함하지 않으면 채택"을 방향 확인 없이 구현하지 마라. 이유: ADR-028 9번 — 반대 방향 배제는 "기각(반대 방향)"이다.
