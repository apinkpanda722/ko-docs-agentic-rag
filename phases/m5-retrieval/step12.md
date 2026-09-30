# Step 12: dev-tuning

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/retrieval/search.py`(`authorized_search`, `SearchParams`), `src/kodocs/retrieval/embedding.py`, `src/kodocs/retrieval/index_build.py`(`m5_index_specs`, `index_name`)
- `src/kodocs/eval/m5_inputs.py`, `src/kodocs/eval/m5_metrics.py`, `src/kodocs/eval/m5_stats.py`
- `data/manifest/m5_index.json`(`chunking`·`indexes`), `data/manifest/m5_eval_inputs.json`
- `docs/ADR.md` ADR-028 10번(튜닝 규칙)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

실험 러너, dev 튜닝, 설정 고정을 만든다. 신규 파일: `src/kodocs/eval/m5_runner.py`, `src/kodocs/eval/m5_dev.py`, `tests/eval/test_m5_runner.py`, `tests/eval/test_m5_dev.py`.

### 1. 러너 `m5_runner.py`

```python
@dataclass(frozen=True)
class RunConfig:
    condition: Literal["composite", "pypdf", "upstage"]
    chunking: Literal["structural", "fixed"]
    method: Literal["bm25", "knn", "hybrid_rrf", "hybrid_weighted"]
    analyzer: Literal["standard", "nori"]
    candidate_chunks: int
    bm25_weight: float | None

@dataclass(frozen=True)
class QuestionRun:
    qid: str
    ranked_docs: tuple[str, ...]          # 문서 순위 전체(권한 필터 적용된 검색 결과)
    top10_chunks: tuple[Span, ...]        # 상위 10 청크 구간
    recall10: float; ndcg10: float
    span_hit: bool | None                 # 정답 구간이 없으면 None
    top1: bool

def all_combo_configs(hybrid: HybridSetting, *, condition="composite") -> list[RunConfig]
    # 실행 14조합: BM25(2 분석기 × 2 청킹) + RRF(4) + 가중합(4) + kNN(청킹당 1회, 분석기 무관 → analyzer는 "nori"로 표기하고 `merged_analyzers=True` 표시)
def run_questions(client, embedder, questions: Sequence[EvalQuestion], cfg: RunConfig) -> list[QuestionRun]
```

- 각 문항은 자기 `asker` 권한(`UserContext(team, grade)`)으로 `authorized_search`를 호출한다. 인덱스 이름은 `index_name(condition, chunking)`.
- 지표는 step 10 함수로만 계산한다. 문서 순위(`ranked_docs`)는 중복 없는 문서 목록이다.
- 진행 줄을 N문항마다 `flush=True`로 출력(FP-034). 문항 하나의 검색 오류는 문항 id와 사유를 남기고 해당 조합을 실패 처리한다(부분 결과로 지표를 계산하지 않는다).

### 2. dev 튜닝 `m5_dev.py`

- **그리드**(고정, 이 step에서 상수로 못박고 ADR-028 옆 변경 기록이 아니라 코드 docstring과 `m5_dev.json`에 기록): hybrid 방식 = `hybrid_rrf`(rank_constant 60 고정) 또는 `hybrid_weighted`(BM25 가중치 `w ∈ {0.3, 0.4, 0.5, 0.6, 0.7}`); 후보 청크 수 `candidate_chunks ∈ {50, 100, 200}` → 총 (1 + 5) × 3 = 18개 설정.
- **평가 조건**: `nori` + `fixed`(조합본 `composite`) 인덱스에서 골드 dev 검색 지표 대상(14)과 실버 dev 검색 지표 대상(43)의 평균 NDCG@10을 각각 구한다. **선택 점수 = (골드 dev 평균 + 실버 dev 평균) / 2**. 동점(부동소수 비교는 소수 10자리 반올림 후)이면 RRF를 우선, 그다음 작은 `candidate_chunks`, 그다음 작은 `bm25_weight`.
- test 문항은 이 모듈에서 어떤 경로로도 읽지 않는다(문항 로더가 dev만 넘긴다). 테스트로 강제한다: dev 튜닝 함수에 test 문항을 넘기면 오류.
- 출력 `data/manifest/m5_dev.json`(커밋): 그리드 표(설정별 골드 dev·실버 dev·선택 점수), 선택된 설정, 문항 수와 기준, 시각, 코드 커밋 해시, 임베더 설정 해시, 색인 매니페스트 해시.
- CLI: `python -m kodocs.eval.m5_dev [--embedder bge-m3|fake]`. 선택 결과만 산출하며 **설정 고정은 별도 명령**이다.

### 3. 설정 고정 `--freeze`

- `python -m kodocs.eval.m5_dev --freeze`: `m5_dev.json`의 선택 결과와 사전 등록 값으로 **`data/manifest/m5_config.json`**을 만든다. 내용: `hybrid_method`, `bm25_weight`(RRF면 null), `candidate_chunks`, `rank_constant: 60`, `fixed_size`·`fixed_overlap`(`m5_index.json`의 `chunking`), 임베더 `model_id`·`revision`·설정 해시, 인덱스 4개의 스키마 해시·청크 수, `m5_eval_inputs.json`의 SHA-256, `m5_dev.json`의 SHA-256, 코드 커밋 해시, 고정 시각, 그리고 정의된 필드 전체의 정규 JSON(키 정렬·UTF-8)에 대한 **`config_hash`**(SHA-256, 이 필드 자신은 제외).
- `m5_config.json`이 이미 있으면 덮어쓰지 말고 오류로 멈춘다(확정 설정은 바꾸지 않는다; 지우는 것은 사용자의 명시적 행동).
- `verify_config(path) -> ConfigCheck`: 해시가 파일 내용과 맞는지, 기록된 `m5_eval_inputs.json`·`m5_dev.json` 해시가 현재 파일과 같은지 검사한다(step 14가 사용). 하나라도 어긋나면 어긋난 항목 목록을 반환한다.

### 4. 테스트

`FakeEmbedder` + `httpx.MockTransport`(또는 `authorized_search`를 대체하는 주입 가능한 검색 함수)로: (a) 14개 조합 생성(kNN 합침 표기 포함), (b) 선택 규칙(동점 처리 3단계), (c) test 문항 거부, (d) 러너가 문항 asker로 검색하는지, (e) 설정 고정 해시 결정론과 재고정 거부, (f) `verify_config`가 변조를 잡는지.

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
   - 그리드가 18설정(RRF 3 + 가중합 5×3)이고 선택 점수 정의가 ADR-028 10번과 같은가?
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
- dev 튜닝에서 test 문항을 읽거나 test 점수를 로그로 남기지 마라. 이유: 튜닝은 dev로만 하고 test는 확정 설정으로 한 번 실행한다(ADR-028 10·11번, ADR-013). 로더 수준에서 막는다.
- 그리드·선택 점수 정의를 결과를 본 뒤 바꾸지 마라. 이유: 사전 등록 규칙이다. 그리드를 넓히거나 선택 점수 가중을 바꾸고 싶으면 `blocked`로 보고한다.
- `m5_config.json`을 자동으로 덮어쓰거나 `--force` 같은 우회를 만들지 마라. 이유: 확정 설정 변경은 사용자의 명시적 결정이다.
- kNN 조합을 분석기별 2회 실행하지 마라. 이유: 벡터 검색은 분석기와 무관하다(ADR-028 2번, 실행 14조합).
- 실제 BGE-M3·OpenSearch로 dev를 실행하지 마라. 이유: 실측은 구현 후 사용자가 실행한다(`--embedder fake`는 로컬 시험용).
