# Step 11: eval-inputs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/eval/golden.py` — `Question`, `Qrel`, `Asker`, `load_questions`, `can_read`, `accessible_relevant_docs`
- `data/golden/queries/gold.jsonl`, `data/golden/queries/silver.jsonl`(각 문항 `split`·`type`·`source`·`qrels`·`tags`·`asker`), `data/manifest/golden_review.json`(split 통계: `retrieval_test_n`, `h7`)
- `data/manifest/table_golden.json` — 표 정답 id → `annex_label`
- `src/kodocs/chunking/corpus.py`, `src/kodocs/chunking/chunkers.py`, `src/kodocs/parsing/annex_mapping.py`, `src/kodocs/retrieval/access.py` — 앞 step 산출물
- `docs/results/2026-09-28-m4-golden-set.md` — 코퍼스 표현 반복 한계(answer_repeated의 배경)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

평가 입력(문항 로더, 정답 구간 매핑, 권한 전제 검사, answer_repeated 태그)을 만든다. 신규 파일: `src/kodocs/eval/m5_inputs.py`, `tests/eval/test_m5_inputs.py`.

### 시그니처

```python
RETRIEVAL_TYPES = ("identifier", "semantic", "table", "multi_doc", "temporal")

@dataclass(frozen=True)
class EvalQuestion:
    question: Question                 # kodocs.eval.golden.Question
    rel_by_doc: dict[str, int]         # doc_relevance(qrels) — rel ≥ 1 문서만 정답, 0은 제외 표기
    answer_ranges: tuple[Span, ...]    # 정답 구간(문서 doc_id + 문자 범위), 조합본 기준
    repeated: bool                     # answer_repeated

def load_retrieval_targets(root: Path = Path(".")) -> dict[str, list[Question]]:
    # 키: "gold_dev", "gold_test", "silver_dev", "silver_test". 대상 = type ∈ RETRIEVAL_TYPES 이고 rel ≥ 1인 qrels가 1개 이상인 문항

def qrel_answer_ranges(q: Question, docs_by_id: Mapping[str, CorpusDoc], *, table_golden: Mapping) -> list[Span]
def answer_repeated(q: Question, docs: Sequence[CorpusDoc]) -> tuple[bool, list[str]]      # (태그, 근거 텍스트 SHA-256 목록)
def check_asker_visibility(qs: Sequence[Question], docs_by_id) -> list[VisibilityFailure]
def main(argv=None) -> int      # CLI: python -m kodocs.eval.m5_inputs → data/manifest/m5_eval_inputs.json
```

### 1. 검색 지표 대상 문항 (무엇을 세는지 고정 — FP-043)

- 세는 단위: **중복 없는 문항 id**. 조건: `type ∈ RETRIEVAL_TYPES`이고 `rel ≥ 1`인 qrels가 1개 이상. permission·unanswerable·sql 유형은 qrels가 비어 있거나 다른 유형이라 제외된다.
- 기대 수: 골드 test **21**, 실버 test **65**(`golden_review.json`의 `split.gold.retrieval_test_n`·`split.silver.retrieval_test_n`과 같아야 한다). 골드 dev·실버 dev 수는 계산해서 manifest에 기록한다(기대: 골드 dev 14, 실버 dev 43 — 다르면 사실 그대로 기록하되 골드 dev가 14가 아니면 `blocked`로 보고). 실제 골드 검색 지표 대상은 dev 6 + test 13 = 19, H7 대상(골드 test 중 identifier·semantic·multi_doc)은 합성 8·실제 6이어야 한다(`golden_review.json`의 `h7`과 비교). 수가 기대와 다르면 조용히 진행하지 말고 오류로 멈춘다.

### 2. 권한 전제 검사 (자동 제외 금지)

- 모든 검색 지표 대상 문항(dev·test, 골드·실버)에 대해, `rel ≥ 1`인 qrels 문서가 (a) 코퍼스에 존재하고 (b) asker에게 보이는지(`kodocs.eval.golden.can_read` 기준, 실제 규정은 public) 전수 검사한다. 하나라도 실패하면 **실패 문항 목록(문항 id, 문서 id, 사유: `missing_doc` | `not_visible`)을 출력하고 종료 코드를 비정상으로** 한다. 그 문항을 자동으로 제외하지 않는다(사람이 판단한다). manifest에도 실패 목록을 기록한다.

### 3. 정답 구간 매핑 (H3·구간 겹침 지표용)

- 합성: qrels의 `start`/`end`를 그대로 쓴다(`CorpusDoc.text == body`이므로). 값이 없는 문서 단위 qrels(구간 없음)는 문서 전체 범위로 보지 말고 "구간 없음"으로 표시한다(구간 겹침 적중률은 구간이 있는 정답에만 정의; 구간 없는 문항 수를 manifest에 기록).
- 실제: `article_key`가 있으면 그 문서 `sections`에서 `kind == "article"`이고 `key`가 같은 **모든** 구간의 범위. `annex_id`(표 정답 id, 예 `17229_0-00`)는 `table_golden.json`에서 `annex_label`을 찾아 정규화 별표 id(step 2·3과 같은 함수)로 바꾼 뒤 `kind == "annex"` 구간의 범위. 매핑 실패(구간을 못 찾음)는 목록으로 모아 오류로 멈춘다(FP-016: 실제 데이터 전체에 돌려 실패를 센 뒤 판단, 자동 제외 금지).
- 정답 구간은 **조합본** 코퍼스 기준으로 계산한다. 이유: H3의 구조 vs 고정 비교와 최종 구성이 조합본 인덱스 위에서 이뤄진다.

### 4. answer_repeated 태그 (결정론)

- 정규화: NFC → 모든 유니코드 공백 제거 → 유니코드 구두점(범주 `P*`) 제거. 그 외 문자는 바꾸지 않는다.
- 근거 텍스트: 합성 = `rel == 2`인 qrels 구간의 `text[start:end]`(각각 하나의 근거). 실제 = `rel == 2`인 qrels의 조 구간 텍스트(또는 별표 구간 텍스트).
- **하나라도 반복이면 참**: 근거 여러 개 중 하나라도 "정규화한 근거 텍스트가 코퍼스의 서로 다른 2개 이상 문서의 정규화 본문에 부분 문자열로 나타남"이면 문항의 `repeated = True`. 문서 수는 문서 단위로 세되 **실제 규정은 같은 `rule_id`(현행·개정 전 버전)를 한 문서로 센다**. 합성 문서는 `doc_id`별.
- 근거가 없는 문항(구간 없음)은 `repeated = False`가 아니라 "태그 불가"로 따로 세어 manifest에 기록한다(FP-021: 분모에 조용히 넣지 않는다). 지금 대상 문항 중 그런 문항이 0이면 0이라고 기록.
- 근거 SHA-256 목록은 정규화한 근거 텍스트의 해시로 기록한다(원문 텍스트는 기록하지 않는다). 정규화 후 길이가 10자 미만인 근거 수도 기록만 한다(규칙으로 쓰지는 않는다).

### 5. manifest `data/manifest/m5_eval_inputs.json` (커밋 대상, CLI가 생성)

문항 id → `{repeated, evidence_sha256, n_matching_docs}`, 골드 test·실버 test × 출처(real/synthetic) × 반복 여부(반복/반복 아님) 그룹별 문항 수 표, 실버 test의 반복/반복 아님 그룹 크기(코퍼스 v2 규칙 4의 10문항 기준 사전 확인 값), 대상 문항 수(위 기준과 무엇을 세는지), 권한 전제 검사 결과, 구간 매핑 실패 목록, 구간 없음 문항 수, 태그 불가 수, 입력 해시(골드·실버 질의 파일, 코퍼스 설정) 요약. **이 파일은 `make m5-test` 전에 확정된다**(ADR-028 13번). CLI를 이 step에서 로컬에서 한 번 실행해 생성한다. 권한 전제 검사나 구간 매핑에서 실패가 나오면 목록과 함께 `blocked`로 보고하라(자동 제외 금지).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.m5_inputs
test -f data/manifest/m5_eval_inputs.json
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - 골드 test 21·실버 test 65, H7 합성 8·실제 6을 `golden_review.json`과 대조하는가?
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
- 권한상 보이지 않는 qrels 문서가 있는 문항을 자동으로 제외하거나 rel을 바꿔 통과시키지 마라. 이유: 문항 정답은 사람이 확정한 golden이며(AGENTS.md), 자동 제외는 분모를 조용히 바꾼다(FP-021). 목록과 함께 멈춘다.
- `data/golden/`의 어떤 파일도 수정·재생성하지 마라. 이유: 확정 기준이다. answer_repeated는 golden이 아니라 `data/manifest/m5_eval_inputs.json`에만 기록한다.
- 검색 지표 대상 수를 하드코딩해 "맞춰서" 통과시키지 마라. 이유: 무엇을 세는지 기준으로 계산하고 `golden_review.json` 값과 대조하는 것이 검증이다(FP-043).
- 실제 문항의 조문·표 원문이나 질문 해설을 manifest에 넣지 마라(해시·id·개수만).
- answer_repeated 정규화를 규칙 밖으로 확장하지 마라(예: 조사 제거, 형태소 분석). 이유: 사전 등록 정의는 공백·문장부호 정규화다.
