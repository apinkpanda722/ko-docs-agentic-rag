# Step 1: golden-schema

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/ARCHITECTURE.md`
- `docs/ADR.md` — ADR-002, ADR-016, **ADR-025**, **ADR-026** (step 0에서 추가)
- `docs/EVAL.md` — 질의셋 절(step 0에서 수정)
- `docs/results/hypotheses.md` (step 0)
- `src/kodocs/synth/seed.py` — `Access`, `Fact`, `Seed`, `load_seed`, `surface_forms`
- `src/kodocs/synth/regulations.py` — `load_index`, `INDEX_PATH`
- `src/kodocs/ingest/manifest.py` — `sha256_file`, `load_records`, `latest_records`
- `data/synthetic/docs/syn-meeting-001.json` — 통과 문서 JSON 형식(`access`, `body`, `fact_spans`)
- `data/manifest/table_golden.json`, `data/manifest/version_matches.jsonl`의 필드 이름

## 배경

M4의 모든 도구가 공유하는 문항·정답(qrels) 스키마와 검증기를 만든다. 이후 step(명세 생성, 초안, 실제 문항, split, 검수·확정)은 이 모듈의 타입과 검증 함수만 쓴다.

## 작업

TDD: `tests/eval/test_golden.py`를 먼저 작성한다. fixture는 `tmp_path`에 작은 문서 JSON·색인을 직접 만든다.

### `src/kodocs/eval/golden.py`

```python
QUESTION_TYPES = ("identifier", "semantic", "table", "multi_doc", "temporal", "permission", "unanswerable", "sql")
TAGS = ("temporal:revision", "temporal:as_of_event", "lexical_mismatch", "canary", "injection")
EXPECTED_RESPONSES = ("answer", "no_answer", "indistinguishable_from_no_answer")
GOLD_QUOTA = {  # (type, source) -> 문항 수. ADR-025 표 그대로
    ("identifier", "real"): 2, ("identifier", "synthetic"): 5, ...  }
SILVER_QUOTA = {"identifier": 25, "semantic": 40, "multi_doc": 35, "temporal": 20, "permission": 15, "unanswerable": 15}
GOLD_SPLIT_QUOTA = {"test": {...}, "dev": {...}}  # ADR-025 test/dev 표 그대로
ATTENTION_CHECK_PREFIX = "chk-"   # 점검용 더미 ID 접두사. 골드·실버·qrels·split에 절대 들어가면 안 된다

@dataclass(frozen=True) class Asker: team: str; grade: int
@dataclass(frozen=True) class Qrel:
    doc_id: str; rel: int                      # 1 | 2
    fact_id: str | None = None; start: int | None = None; end: int | None = None; span_sha256: str | None = None   # 합성
    article_key: str | None = None; annex_id: str | None = None                                                   # 실제
@dataclass(frozen=True) class SqlAnswer: query: str; query_sha256: str; expected: object; db_content_hash: str
@dataclass(frozen=True) class Question:
    id: str; set: str; source: str; type: str; tags: tuple[str, ...]
    question: str; asker: Asker; as_of: str | None
    qrels: tuple[Qrel, ...]; anchor_values: tuple[str, ...]; answer_values: tuple[str, ...]
    expected_response: str; sql: SqlAnswer | None
    reference_answer: str | None          # 합성만. 실제는 None
    reference_sha256: str | None          # 실제만(interim 참조 답변의 해시)
    summary80: str | None                 # 실제만(80자 이하 요지)
    split: str | None                     # "dev" | "test" | None(확정 전)

def question_from_dict(d: Mapping) -> Question: ...
def question_to_dict(q: Question) -> dict: ...          # 결정론 JSON(키 정렬)
def load_questions(path: Path) -> list[Question]: ...   # JSONL
def write_questions(path: Path, questions: Sequence[Question]) -> None: ...  # 임시 파일 → replace
def span_sha256(text: str) -> str: ...
def can_read(asker: Asker, access: Mapping) -> bool: ...  # public: 전원, team: teams 포함, restricted: teams 포함 그리고 grade >= min_grade
def accessible_relevant_docs(q: Question, docs: Mapping[str, Mapping]) -> list[str]: ...
def validate_question(q: Question, *, docs: Mapping[str, Mapping], regulation_index: Mapping, table_ids: set[str]) -> list[str]: ...  # 오류 코드 목록
def quota_report(questions: Sequence[Question], quota: Mapping) -> dict: ...   # {key: {expected, actual, diff}}
```

`validate_question` 오류 코드(각각 fixture 테스트 필수):

| 코드 | 판정 |
|---|---|
| `unknown_type` / `unknown_tag` / `unknown_expected_response` | 열거값 밖 |
| `attention_check_id` | id가 `chk-`로 시작(점검용 더미는 이 모듈의 어떤 파일에도 들어가면 안 됨) |
| `span_mismatch` | 합성 qrel의 `docs[doc_id]["body"][start:end]`의 SHA-256 ≠ `span_sha256`, 또는 그 `(fact_id,start,end)`가 문서 `fact_spans`에 없음 |
| `unknown_doc` | 합성 qrel의 `doc_id`가 통과 문서에 없음 |
| `unknown_article` | 실제 qrel `doc_id`가 `law-{serial}` 형식이 아니거나, 그 serial의 규정에 `article_key`가 색인에 없음(색인은 규정명 → articles, `serial` 필드로 찾음) |
| `unknown_annex` | 실제 qrel의 `annex_id`가 `table_ids`에 없음 |
| `bad_rel` | rel이 1·2가 아님 |
| `empty_qrels` | `expected_response == "answer"`인데 관련도 2 qrel이 없고 sql도 없음 |
| `nonempty_qrels_for_no_answer` | `no_answer`인데 qrels가 있음 |
| `permission_accessible` | `indistinguishable_from_no_answer`인데 질문자가 읽을 수 있는 관련 문서가 1개 이상 |
| `answer_inaccessible` | `answer`(합성)인데 질문자가 읽을 수 있는 관련도 2 문서가 0개 |
| `temporal_tag` | type이 temporal인데 `temporal:revision`·`temporal:as_of_event` 중 정확히 하나가 아님, 또는 as_of_event인데 `as_of`가 없음 |
| `real_text_fields` | source가 real인데 `reference_answer`가 있음, 또는 `summary80`이 없거나 80자 초과(ADR-002) |
| `sql_missing` | type이 sql인데 `sql`이 None |

### 원문 비커밋 가드 테스트

- `tests/eval/test_golden.py`에 테스트 추가: 커밋 대상 경로 `data/golden/queries/`·`data/synthetic/queries/`의 JSONL이 있으면 모든 real 문항이 `reference_answer is None`이고 `summary80` ≤ 80자임을 검사한다(파일이 없으면 통과).
- 점검용 더미 가드: 같은 경로의 모든 파일(JSONL·JSON·CSV)에 `chk-` 접두사 ID가 없음을 검사하는 테스트를 추가한다. 단 `data/golden/*review*.csv`처럼 검수 CSV는 step 7에서 별도 규칙을 둔다 — 이 step에서는 `data/golden/queries/`와 `data/synthetic/queries/`만 검사한다.

## Acceptance Criteria

```bash
make verify
uv run python -c "from kodocs.eval.golden import GOLD_QUOTA, SILVER_QUOTA, GOLD_SPLIT_QUOTA; assert sum(GOLD_QUOTA.values())==50 and sum(SILVER_QUOTA.values())==150 and sum(GOLD_SPLIT_QUOTA['test'].values())==30 and sum(GOLD_SPLIT_QUOTA['dev'].values())==20"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 할당 상수가 ADR-025 표와 칸마다 같은가(FP-043)?
   - `retrieval` 패키지를 import하지 않았는가? (`eval`은 검색 구현 전 단계다)
   - 모든 오류 코드에 fixture 테스트가 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 검증 실패를 자동으로 고치는 함수(span 재계산, 요지 자르기 등)를 만들지 마라. 이유: 정답 데이터를 조용히 바꾸면 검수한 것과 달라진다(FP-020).
- 청크 ID를 qrel 필드로 두지 마라. 이유: ADR-016(청킹 방식마다 청크가 달라 비교가 무효).
- `data/golden/`에 파일을 만들지 마라. 이유: 사람만 확정한다(AGENTS.md).
- 조문 본문을 fixture로 커밋하지 마라. 테스트용 규정 텍스트는 짧은 가짜 문장으로 만든다. 이유: ADR-002.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
