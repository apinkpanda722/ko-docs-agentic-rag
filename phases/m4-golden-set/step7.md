# Step 7: review-packets

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — `data/golden/`은 사람이 검수해 확정한 기준, 에이전트는 수정하지 않는다, `make verify`가 유일한 검증 진입점
- `docs/ADR.md` — ADR-002, ADR-013, ADR-015, **ADR-025**(검수: Claude 판정 선채움·기본값 수락·행별 서명·점검용 더미), **ADR-026**
- `docs/EVAL.md` — 질의셋 절, 결과 보고 규칙(파일명 날짜, 커밋 해시, 고정 조건)
- step 1: `src/kodocs/eval/golden.py`, step 2: `golden_specs.py`, step 3·4: `query_draft.py`·`lexical.py`, step 5: `golden_real.py`, step 6: `golden_split.py`
- `src/kodocs/eval/synth_review.py` — M3 검수 도구(표본·CSV 양식·집계, `data/golden/` 아래에 파일을 만들지 않는 규칙), 형식 참고용
- `src/kodocs/eval/synth_report.py` — 고정 결과 문서 + 생성 영역 교체 방식(`write_report`, `BEGIN`/`END` 마커)
- `src/kodocs/eval/templates/m4_golden.md` (step 0)

## 배경

골드 50은 전부, 실버는 층화 20%(30문항)를 사람이 검수한다. 방식은 "Claude 판단 초안 + 사람 확인"이고 기본값 수락이다: 패킷에 Claude 판정을 미리 채워 두고, 사람은 동의하지 않는 항목만 적는다. 대신 사람이 실제로 본 행과 안 본 행을 구별하려고 행마다 `reviewer`·`reviewed_on`을 **사람이** 적어야 검수로 인정한다. 도구는 이 두 칸을 절대 미리 채우지 않는다. 선택 기능으로 Claude 판정을 일부러 틀리게 한 점검용 더미 3개를 섞어 적발 수를 보고한다. 확정 명령은 사용자가 실행하며 검수 결과로 골드·실버 파일을 `data/golden/queries/`에 만든다.

## 작업

TDD: `tests/eval/test_golden_review.py`를 먼저 작성한다.

### 1. `src/kodocs/eval/golden_review.py`

```python
PACKETS_DIR = Path("data/interim/golden/packets")                  # 비커밋(실제 문항 패킷에 조문 발췌가 들어감)
CLAUDE_VERDICTS = Path("data/interim/golden/claude_verdicts.jsonl") # 비커밋: Claude 판정 초안(세션에서 작성)
TEMPLATE_CSV = Path("data/interim/golden/review_template.csv")     # 비커밋
ATTENTION_KEY = Path("data/interim/golden/attention_checks.json")  # 비커밋: 더미 정답표
REVIEW_GLOB = "data/golden/m4_review/*-review.csv"                 # 사람이 저장하는 검수 결과(커밋)
GOLD_PATH = Path("data/golden/queries/gold.jsonl")                 # 확정 산출물(커밋)
SILVER_PATH = Path("data/golden/queries/silver.jsonl")
SUMMARY_PATH = Path("data/manifest/golden_review.json")
REVIEW_ITEMS = ("q1_answerable", "q2_qrels_complete", "q3_labels_correct")   # 3개 이내
CSV_COLUMNS = ("question_id", "set", "type", "claude_q1", "claude_q2", "claude_q3", "claude_note",
               "reviewer", "reviewed_on", "disagree", "correction")
def silver_sample(silver: Sequence[Question], *, random_seed: int) -> list[Question]: ...   # 유형별 층화 20% = 30문항
def build_packets(root: Path, *, attention_checks: int = 0) -> int: ...
def promote(root: Path) -> int: ...
def main(argv=None) -> int: ...   # python -m kodocs.eval.golden_review {packets [--attention-checks N]|promote}
```

**패킷(`packets`)**
- 입력: 합성은 `data/synthetic/queries/validated.jsonl`, 실제는 `data/interim/golden/real_drafts.jsonl`, Claude 판정은 `CLAUDE_VERDICTS`(`{question_id, q1, q2, q3, note}`, 없으면 판정 칸을 `(초안 없음)`으로 두고 경고).
- 문항 1개 = Markdown 한 섹션: 질문, 질문자(팀·직급), 유형·태그·기준일, 기대 응답, 정답 구간(관련도 2는 앞뒤 ±200자 발췌 + 구간 `**굵게**`, 관련도 1은 문서 제목만), 미표시 후보 문서 ID, 어휘 겹침 수치, SQL이면 쿼리와 기대 값, 그리고 3항목의 Claude 판정(선채움). 실제 문항은 조문 발췌를 넣는다(그래서 `data/interim/`).
- `TEMPLATE_CSV`: 골드 50 + 실버 표본 30(+ 점검용 더미) 행. `claude_*`는 판정으로 채우고 **`reviewer`·`reviewed_on`·`disagree`·`correction`은 빈칸**. 이 CSV를 사람이 채워 `data/golden/m4_review/{검수일}-review.csv`로 저장한다(도구는 `data/golden/` 아래에 쓰지 않는다).
- **점검용 더미(선택, `--attention-checks N`, 권장 3)**: 기존 합성 문항을 복제해 ID를 `chk-NNN`으로 바꾸고, 3항목 중 1개의 Claude 판정을 사실과 반대로 넣은 문항. 어떤 항목을 틀리게 했는지는 `ATTENTION_KEY`에만 기록. 패킷·CSV에서 일반 문항과 같은 형식으로 섞는다(순서는 결정론 셔플).

**확정(`promote`, 사용자 실행)**
- 검수 CSV를 읽는다. **`reviewer`와 `reviewed_on`(YYYY-MM-DD)이 모두 있는 행만 검수된 행**이다. 빈 행은 수락이 아니라 "미검수"로 센다.
- `disagree` 빈칸 = Claude 판정 수락. 값(`1`,`2`,`3` 쉼표 구분)이 있으면 해당 항목 불일치 + `correction` 필수(비면 그 행 무효). 한 문항이 여러 CSV에 있으면 `reviewed_on`이 가장 늦은 행.
- 문항별 최종 판정 = Claude 판정에 사람 불일치를 반영한 값. 3항목이 모두 긍정인 문항만 확정한다. 정답 구간 보완(q2 불일치 + correction에 `add:doc_id` 또는 `add:fact_id`, `remove:...`)은 명시된 형식만 기계적으로 반영하고 형식이 아니면 그 문항을 "수정 필요"로 확정에서 뺀다.
- 골드: 50문항 **전부** 검수·확정되어야 쓴다. 하나라도 미검수·수정 필요·부정이면 골드 파일을 쓰지 않고 목록을 출력하고 종료 코드 1. 확정 시 `golden_split.assign_split(..., GOLD_SPLIT_QUOTA)`로 split을 채우고 실제 문항은 투영(참조 답변 제거, `reference_sha256`) 형태로 쓴다.
- 실버: 150문항 전부를 쓰되 표본 30의 검수 결과(표본 오류율 = 부정 판정 비율과 Wilson 95% 구간)를 요약에 기록하고, 표본에서 부정인 문항은 실버에서 뺀다. split은 실버 규칙(유형별 60/40).
- **불변성(FP-020)**: `GOLD_PATH`가 이미 있고 새로 만들 내용과 다르면 덮어쓰지 않고 종료 코드 1("골드는 확정 후 수정하지 않는다"). 같으면 그대로 둔다.
- **점검용 더미 격리**: `chk-` 문항은 골드·실버 파일·qrels·split 어디에도 쓰지 않는다. 적발 = 사람이 `disagree`에 틀리게 넣은 항목 번호를 적은 경우. 요약에 `attention_checks: {n, detected, missed_ids}`.
- 요약 `SUMMARY_PATH`(결정론): `{gold: {reviewed, confirmed, disagree_by_item, acceptance_rate}, silver_sample: {n, negative, error_rate, wilson95}, attention_checks, split: split_report(...), inputs: {파일 해시}}`.

### 2. 결과 문서

- `src/kodocs/eval/m4_report.py`: `docs/results/2026-09-28-m4-golden-set.md` **하나로 고정**(실행일로 파일명을 만들지 않는다). 없으면 `templates/m4_golden.md`로 만들고, 있으면 생성 영역만 교체한다(M3 `synth_report.write_report`와 같은 방식). 생성 영역: 실행 정보(커밋 해시·날짜), 유형 × 출처 × split 표, **시점 태그별(temporal:revision / temporal:as_of_event) 수**, qrels 통계(문항당 관련도 2 문서 수, 미표시 후보 수), 어휘 겹침 분포와 확정 상한, 검수 결과(수락률·항목별 불일치·실버 표본 오류율·점검용 더미 적발 수), split 보고(그룹·할당 차이·누수 문서·가설 판정 문항 수 `retrieval_test_n`·`h5_bundle_test_n`·`h7`), 비용(질의 초안 Batch 원장 합계, `golden_dry_runs.jsonl` 추정 대비 실제, 콘솔 대조 칸은 사용자 기록).
- 입력이 없는 칸은 `(실행 후 기록)`.

### 3. 가드 테스트(필수)

- **서명 선채움 금지**: `build_packets`가 만든 `TEMPLATE_CSV`의 모든 행에서 `reviewer`·`reviewed_on`·`disagree`·`correction`이 빈 문자열(점검용 더미 행 포함).
- 서명 없는 행은 기본값 수락으로 치지 않고 "미검수"로 센다 → 골드 확정 실패.
- **더미 격리**: 더미 3개를 섞고 모두 서명·수락한 CSV로 `promote` → 골드·실버 파일과 split 보고 어디에도 `chk-`가 없음, `attention_checks.detected == 0`. 더미의 틀린 항목을 `disagree`에 적은 CSV → `detected == 3`.
- 커밋 경로 전체(`data/golden/queries/`, `data/synthetic/queries/`, `data/manifest/golden_*.json`) 스캔 테스트: `chk-` 없음(파일이 없으면 통과).
- 골드 50 중 1개라도 미검수 → 골드 파일 미작성, 종료 코드 1.
- 기존 골드와 다른 내용으로 `promote` → 덮어쓰지 않음, 종료 코드 1.
- 실제 문항이 골드 파일에 `reference_answer` 없이 투영 형태로만 들어감.
- 결과 문서가 고정 경로 하나에만 쓰이고 생성 영역 밖 내용이 보존됨.

### 4. Makefile·AGENTS.md

- `golden-packets: uv run python -m kodocs.eval.golden_review packets --attention-checks 3`
- `golden-promote: uv run python -m kodocs.eval.golden_review promote && uv run python -m kodocs.eval.m4_report`
- `m4-report: uv run python -m kodocs.eval.m4_report`
- AGENTS.md:
  - `make golden-packets  # 골드 50·실버 표본 30 검수 패킷(Claude 판정 선채움, 점검용 더미 3) → data/interim/golden/ (로컬 전용, 네트워크 없음)`
  - `make golden-promote  # data/golden/m4_review/*-review.csv 집계 → 확정 골드·실버 data/golden/queries/ + 결과 문서 — 사용자 실행`
  - `make m4-report       # M4 결과 문서 docs/results/2026-09-28-m4-golden-set.md(고정) 갱신 (네트워크 없음)`

## Acceptance Criteria

```bash
make verify
grep -n "golden-packets\|golden-promote\|m4-report" Makefile AGENTS.md
test ! -e data/golden/queries/gold.jsonl && echo "gold not written by step"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `golden-packets`·`golden-promote`를 실제 데이터로 실행하지 마라(검수 CSV가 없고, 골드는 사람이 확정한다).
2. 아키텍처 체크리스트를 확인한다:
   - 도구가 `reviewer`·`reviewed_on`을 채우는 코드 경로가 없고 테스트가 강제하는가?
   - 점검용 더미가 골드·실버·qrels·split에 들어갈 수 없음을 테스트가 강제하는가?
   - 기존 골드를 덮어쓰지 않는가(FP-020)? 실제 문항 참조 답변이 커밋 경로로 나가지 않는가(ADR-002)?
   - 결과 문서가 고정 파일 하나인가, 측정 환경·커밋 해시를 적는가(FP-044)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `reviewer`·`reviewed_on`을 미리 채우거나 기본값을 넣지 마라(현재 사용자 이름·오늘 날짜 포함). 이유: 사람이 실제로 본 행과 안 본 행을 구별하는 유일한 근거다(ADR-025).
- `data/golden/` 아래에 이 step에서 파일을 만들지 마라. 확정 파일은 사용자가 `make golden-promote`로 만든다. 이유: AGENTS.md(golden은 사람이 확정).
- 점검용 더미를 골드·실버 파일, qrels, split, 커밋 경로 manifest에 쓰지 마라. 이유: 평가 문항이 오염된다.
- 확정된 골드 파일을 덮어쓰는 옵션(`--force` 등)을 만들지 마라. 이유: FP-020, "수치를 맞추려고 golden을 바꾸지 않는다"(AGENTS.md).
- 조문 발췌가 들어가는 패킷을 커밋 경로에 쓰지 마라. 이유: ADR-002.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
