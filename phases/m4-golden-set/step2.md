# Step 2: synth-specs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/ADR.md`(ADR-016, ADR-024, **ADR-025**), `docs/EVAL.md`(질의셋 절)
- step 1 산출물: `src/kodocs/eval/golden.py`(`Question`, `Qrel`, `SqlAnswer`, `Asker`, `can_read`, `validate_question`, `GOLD_QUOTA`, `SILVER_QUOTA`)
- `src/kodocs/synth/seed.py` — `load_seed`, `Fact`(`subject`, `date`, `kind`, `value`), `surface_forms`, `Person`, `TEAMS`
- `src/kodocs/synth/db.py` — `build_rows`, SQLite 스키마(tickets·approvals·incidents), `DB_PATH`
- `src/kodocs/synth/identifiers.py` — `extract_identifiers`
- `data/synthetic/docs/*.json` — 통과 문서(`access`, `body`, `fact_spans`, `canary`, `injection`)
- `data/synthetic/canaries.json`, `data/synthetic/injections.json`
- `data/manifest/synth_db.json` — `content_hash`
- `src/kodocs/ingest/manifest.py` — `sha256_file`

## 배경

합성 문항(골드 합성 30 + 실버 150)의 **명세**를 LLM 없이 결정론으로 만든다. 명세 = 질문 문장만 빈 `Question`(유형·태그·질문자·기준일·qrels·주어 값·답 값·기대 응답·SQL 정답·참조 답변). 질문 문장은 step 3이 Batch로 쓴다. 문서가 사실을 공유하므로 qrels는 fact span을 가진 모든 문서를 포함해야 Recall이 부당하게 깎이지 않는다.

## 작업

TDD: `tests/eval/test_golden_specs.py`를 먼저 작성한다. 작은 가짜 시드(`Seed` 직접 구성)와 문서 3~5개 fixture로 각 유형을 검증하고, 실제 저장소 데이터로 도는 통합 테스트 1개(할당 충족·검증 오류 0)를 둔다.

### `src/kodocs/eval/golden_specs.py`

```python
SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")        # 커밋
SUMMARY_PATH = Path("data/manifest/golden_specs.json")          # 커밋, 결정론
def fact_index(docs: Mapping[str, Mapping]) -> dict[str, list[tuple[str, int, int]]]: ...   # fact_id -> [(doc_id, start, end)]
def unmarked_mentions(fact: Fact, docs, people, marked: set[str]) -> list[str]: ...          # 원자 사실 허용 표기가 표시 없이 나온 문서 ID
def build_qrels(fact_ids: Sequence[str], *, seed, docs, index) -> tuple[Qrel, ...]: ...       # rel2: span 문서, rel1: 같은 subject만 다루는 문서
def build_specs(seed: Seed, docs: Mapping[str, Mapping], *, db_rows, db_content_hash: str, random_seed: int) -> list[Question]: ...
def run(root: Path, *, print_fn=print) -> int: ...
def main(argv=None) -> int: ...   # python -m kodocs.eval.golden_specs
```

**qrels 규칙**
- 관련도 2: 답 사실 `fact_id`의 span을 가진 **모든** 통과 문서(한 문서에 span이 여러 개면 모두 qrel로).
- 관련도 1: 답 사실과 같은 `subject`의 다른 사실 span은 있지만 답 사실 span은 없는 문서.
- 미표시 언급: 원자 사실(`kind`가 text가 아닌 것)의 `surface_forms`가 표시 없이 본문에 나온 문서 ID를 명세의 `unmarked_candidates` 필드(요약·패킷용, qrels 아님)로 남긴다. qrels에 자동으로 넣지 마라. 사람이 검수에서 정한다.

**질문자(asker)**: 시드 인물에서 결정론(`random.Random(random_seed + 문항 번호)`)으로 고른다. answer 문항은 관련도 2 문서 중 1개 이상을 읽을 수 있는 인물, permission 문항은 관련 문서(1·2 모두)를 하나도 읽을 수 없는 인물.

**유형별 명세 생성(각 유형 fixture 테스트 필수)**
- identifier: 주어 = `id` 사실 값(티켓·결재·장애 ID). 답 = 같은 subject의 다른 사실 1개. `anchor_values`에 ID, `answer_values`에 답 사실의 허용 표기 전부(서술 사실이면 사실 문장).
- semantic: 주어 = 서비스명 또는 인물(ID 없이). 답 = 서술 사실(결정·정책·원인·계획 등). 주어만으로 답 사실이 하나로 정해지지 않으면(같은 서비스·같은 속성 사실이 2개 이상) 그 후보는 쓰지 않는다.
- multi_doc: 같은 subject 또는 같은 서비스의 사실 2개 이상. **어느 한 문서도 모든 답 사실 span을 갖지 않아야** 한다(검사 필수). qrels는 답 사실별 규칙의 합집합.
- temporal(`temporal:as_of_event`): 주어 = 서비스 + `as_of`(일 단위 날짜). 답 = 그 날짜로만 특정되는 사건의 사실. **기준일을 빼면 후보 사건(같은 서비스·같은 속성)이 2개 이상**이어야 채택(검사 필수). 실버 temporal 20 = 사건 19 + 값 교체 1. 값 교체 1은 identity-api 담당자(2026-03-28 p-002 → 2026-05-09 p-133)처럼 **같은 서비스의 같은 속성 값이 날짜에 따라 바뀐** 경우를 시드에서 찾아 as_of를 두 날짜 사이로 둔다(시드에서 정확히 찾고, 없으면 실패로 멈춰라 — 추측해 만들지 마라).
- permission: 답 사실의 모든 관련 문서가 질문자에게 보이지 않는 경우. `expected_response="indistinguishable_from_no_answer"`. 제한 문서의 카나리아 사실을 답으로 쓰면 태그 `canary`.
- unanswerable: 존재하는 주어(서비스·장애)에 대해 시드에 없는 속성(예: 비용·영향 사용자 수처럼 시드 사실 종류에 없는 것)을 묻는 명세. `expected_response="no_answer"`, qrels 없음. 확인 ①(해당 subject에 그 속성 사실 없음)·②(통과 문서 전체 본문에서 속성 핵심어 + 주어 동시 등장 없음)을 수행하고 결과를 `no_answer_checks: {seed: bool, docs: bool, real: null}`로 남긴다(③ 실제 규정 검색은 step 5가 채운다).
- sql(골드만 6): `db.build_rows(seed)`로 메모리 SQLite를 만들어 집계 SELECT(팀별·상태별·월별 건수, 결재 금액 합 등) 6개를 실행하고 기대 값을 기록. 쿼리는 코드에 템플릿으로 고정. `db_content_hash`는 `synth_db.json` 값.
- 인젝션 문서가 관련도 2인 문항에는 태그 `injection`.

**할당**: 골드 합성은 `GOLD_QUOTA`의 synthetic 칸, 실버는 `SILVER_QUOTA`를 정확히 채운다. 골드와 실버가 **같은 답 사실을 공유하지 않게** 한다(골드 우선 배정). ID: 골드 `g-syn-NNN`, 실버 `s-syn-NNN`. 후보가 할당보다 적은 유형이 있으면 **실패로 멈추고** 유형·후보 수를 출력한다(FP-031). 조용히 다른 유형으로 채우지 마라.

**참조 답변**: 합성은 답 사실 값을 이은 짧은 문장으로 `reference_answer`를 채운다(서술 사실은 사실 문장 그대로).

**산출과 요약**
- 모든 명세를 `golden.validate_question`에 통과시킨다(질문 문장이 빈 상태는 허용하도록 이 단계에서만 `question=""`를 예외 처리). 오류가 하나라도 있으면 종료 코드 1.
- `SPECS_PATH`: 명세 JSONL(키 정렬, id 정렬). `SUMMARY_PATH`: `{counts: {set: {type: n}}, tags: {...}, unmarked_candidate_questions: n, rejected_candidates: {reason: n}, inputs: {seed_files_sha256, docs_sha256(문서 JSON 해시들의 해시), db_content_hash}}`(시각 없음).
- 같은 입력으로 두 번 실행 → 두 파일 바이트 동일(테스트 필수).

### Makefile·AGENTS.md
- `query-specs: uv run python -m kodocs.eval.golden_specs` (`.PHONY` 추가)
- AGENTS.md 명령어 절: `make query-specs    # 합성 문항 명세(골드 합성 30·실버 150, qrels 결정론) → data/synthetic/queries/specs.jsonl (네트워크 없음)`

## Acceptance Criteria

```bash
make verify
make query-specs && uv run python -c "import json;d=json.load(open('data/manifest/golden_specs.json'));print(d['counts'])"
grep -n "query-specs" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `query-specs`는 네트워크 없이 저장소 데이터만 읽는다.
2. 아키텍처 체크리스트를 확인한다:
   - 골드 합성 30·실버 150이 유형별 할당과 정확히 같은가(FP-043)?
   - multi_doc의 "한 문서가 모든 답 사실을 갖지 않음", temporal의 "기준일 없이는 후보 2개 이상" 검사가 테스트로 강제되는가?
   - 할당 부족 시 실패로 멈추는가(FP-031)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(유형별 문항 수 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- LLM을 호출하지 마라. 이유: 정답은 시드·문서에서 결정론으로 나와야 한다(EVAL 원칙).
- 미표시 언급을 qrels에 자동으로 넣지 마라. 이유: 표기가 같아도 다른 맥락일 수 있어 사람이 정한다(ADR-025).
- `data/synthetic/docs/`·시드·계획·레지스트리를 수정하지 마라. 이유: M3에서 확정한 코퍼스다.
- `data/golden/`에 파일을 만들지 마라. 이유: 사람만 확정한다(AGENTS.md). 명세는 `data/synthetic/queries/`에 둔다.
- 할당을 못 채운 유형을 다른 유형 문항으로 메우지 마라. 이유: 가설 판정 문항 수가 유형 할당에 묶여 있다(FP-031, FP-043).
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
