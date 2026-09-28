# Step 8: review-tools

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL: `data/golden/`은 사람이 확정한 기준이며 에이전트는 수정하지 않는다
- `docs/EVAL.md` — "합성 코퍼스 신뢰성"(표본 20건 사람 검수: 사실 일관성·문체 다양성·현실감)
- `docs/PRD.md` — M3 사용자 작업(`make generate` 실행, 합성 문서 표본 20건 검수)
- step 3 산출물: `data/synthetic/plan.jsonl`
- step 5 산출물: `src/kodocs/synth/generate.py`, Makefile `generate`·`generate-pilot`
- step 6 산출물: `src/kodocs/synth/validate.py`, `data/synthetic/docs/`
- step 7 산출물: `src/kodocs/eval/synth_metrics.py`, `src/kodocs/eval/synth_report.py`, `src/kodocs/eval/templates/m3_synthetic.md`(7절 사람 검수 자리)

## 배경

사용자가 합성 문서 20건을 검수할 수 있게 **층화 표본 추출·검수 템플릿·집계 도구**를 만들고, `make generate`를 "생성/수거 → 검증 → 지표 → 결과 문서" 순서로 연결한다. 검수 자체는 사용자가 한다. 사람이 채운 검수 결과는 `data/golden/synthetic_review/`에 두며, 도구는 읽기만 한다.

## 작업

TDD: `tests/eval/test_synth_review.py`를 먼저 작성한다.

### 1. `src/kodocs/eval/synth_review.py`

```python
SAMPLE_SIZE = 20
SAMPLE_PATH = Path("data/synthetic/review/sample.json")            # 커밋 (표본 목록·추출 근거)
TEMPLATE_PATH = Path("data/synthetic/review/review_template.csv")  # 커밋 (빈 검수 양식)
GUIDE_PATH = Path("data/synthetic/review/README.md")               # 커밋 (검수 안내·체크리스트)
RESULTS_DIR = Path("data/golden/synthetic_review")                 # 사용자가 채운 CSV를 둔다. 도구는 읽기만
SUMMARY_PATH = Path("data/manifest/synth_review.json")             # 커밋, 집계 결과
def draw_sample(docs: Sequence[Mapping], *, random_seed: int, size: int = SAMPLE_SIZE) -> list[dict]: ...
def write_templates(root: Path) -> int: ...     # python -m kodocs.eval.synth_review sample
def score(root: Path) -> int: ...               # python -m kodocs.eval.synth_review score
def main(argv=None) -> int: ...
```

**층화 표본(결정론)**

- 모집단: 검증을 통과한 문서(`data/synthetic/docs/*.json`). 20건 미만이면 종료 코드 1(FP-031).
- 층: 유형 6 × 권한 3. 먼저 **유형마다 최소 3건**(6 × 3 = 18), **권한 수준마다 최소 4건**을 만족시키고, 나머지 2건은 모집단 비율이 큰 층에서 채운다. 인젝션 문서 1건 이상, 카나리아 문서(제한) 4건 이상 포함. 조건을 만족할 수 없으면(모집단 부족) 가능한 최대로 뽑고 부족한 조건을 `sample.json`의 `shortfalls`에 기록한다.
- 난수: `random.Random(company.random_seed + 고정 오프셋)`. 같은 모집단이면 같은 표본.
- `sample.json`: `{"size", "random_seed", "population_count", "strata_counts": {...}, "shortfalls": [...], "docs": [{"doc_id","doc_type","access_level","path","fact_ids"}]}`.

**검수 양식 `review_template.csv`**(UTF-8 BOM 없이, 한 행 = 한 문서, `doc_id` 등은 미리 채움)

열: `doc_id, doc_type, access_level, reviewer, reviewed_on, fact_consistency, fact_errors, style_natural, style_notes, realism, realism_notes, injection_visible, other_issues`
- `fact_consistency`: `1`(배정 사실이 시드·다른 문서와 모순 없음) / `0`(모순 있음). `0`이면 `fact_errors`에 fact_id와 설명
- `style_natural`: `1`~`3`(1 어색·틀에 박힘, 2 보통, 3 실제 사내 문서 같음)
- `realism`: `1`~`3`(업무 맥락·용어·구성의 현실감)
- `injection_visible`: 인젝션 문서만 `y`/`n`(문구가 문서 흐름에서 눈에 띄게 이질적인가), 나머지는 빈칸

**검수 안내 `README.md`**: 문서 JSON을 보는 방법(`body`, `fact_spans`로 사실 위치 확인), 체크리스트 3항목의 판정 기준과 예시, 완료된 CSV를 `data/golden/synthetic_review/{검수일}-review.csv`로 저장하라는 안내, 문서 본문을 고치지 말라는 안내.

**집계 `score`**

- `RESULTS_DIR/*.csv`를 읽는다(여러 파일이면 `reviewed_on`이 가장 최근인 행을 `doc_id`별로 사용). 결과 파일이 없으면 "검수 결과 없음" 안내 후 종료 코드 1.
- 검사: `doc_id`가 표본에 있음, 값 범위(`0/1`, `1~3`, `y/n/빈칸`), 누락 문서. 형식 위반 행은 사유와 함께 보고하고 집계에서 뺀다(전체를 멈추지 않음, FP-030).
- 집계: 검수 완료 수/20, 사실 일관성 비율, 문체·현실감 평균과 분포(유형별·권한별), 사실 오류 목록(fact_id), 인젝션 이질감 y 비율.
- `SUMMARY_PATH`에 결정론 JSON으로 쓰고, `synth_report`가 7절을 채울 수 있게 한다: step 7의 `render_tables`가 `synth_review.json`이 있으면 7절 수치를 렌더링하도록 **`synth_report.py`에 7절 렌더링만 추가**한다(다른 절은 건드리지 않는다).

### 2. `make generate` 연결 — Makefile

- `generate`를 다음 순서로 바꾼다. 생성 단계가 "진행 중"(종료 코드 0이지만 수거 전)일 때 뒤 단계가 에러로 끝나지 않게 한다:
  - `uv run python -m kodocs.synth.generate` (dry-run 표시 → 확인 → 제출, 또는 조회·수거)
  - 수거가 끝난 문서가 1건 이상일 때만 `uv run python -m kodocs.synth.validate` → `uv run python -m kodocs.eval.synth_metrics` → `uv run python -m kodocs.eval.synth_report`
  - 구현 방식은 재량(예: `generate.py`에 `--then-validate` 옵션을 두어 수거 후 파이썬 안에서 이어 호출, 또는 `generate.py`가 "수거 완료" 상태를 종료 코드 3 같은 약속된 값으로 알리고 Makefile이 분기). **선택한 방식을 테스트로 고정**한다: 진행 중 → 검증 미실행, 수거 완료 → 검증·지표·보고서 실행.
- `review-sample: uv run python -m kodocs.eval.synth_review sample`
- `review-score: uv run python -m kodocs.eval.synth_review score`
- `.PHONY` 추가. AGENTS.md 명령어 절:
  - `make generate` 설명을 `# 합성 문서 Batch 생성(재실행 시 조회·수거) → 수거 후 검증·지표·결과 문서 — 실제 API 호출·과금, 사용자 실행`으로 갱신
  - `make review-sample  # 검증 통과 문서에서 유형·권한 층화 표본 20건 + 검수 양식 → data/synthetic/review/ (네트워크 없음)`
  - `make review-score   # data/golden/synthetic_review/*.csv 사람 검수 결과 집계 → data/manifest/synth_review.json·결과 문서 7절 (네트워크 없음)`

### 3. `docs/ARCHITECTURE.md`

- `data/golden/` 설명 줄에 `synthetic_review/`(합성 문서 표본 사람 검수 결과, 사람만 작성)를 추가한다. 다른 줄은 바꾸지 않는다.

### 4. 테스트 (필수)

- 표본 층화: fixture 모집단(유형 6 × 권한 3, 층마다 2~5건)에서 20건, 유형마다 ≥3, 권한마다 ≥4, 인젝션 ≥1, 제한 ≥4. 두 번 추출 → 동일.
- 모집단 부족 fixture → `shortfalls` 기록, 모집단 20건 미만 → 종료 코드 1.
- 양식 CSV 열 순서·미리 채운 값.
- 집계: 정상 CSV → 비율·평균 값 정확. 범위 밖 값 1행 → 그 행만 제외·보고. 결과 파일 없음 → 종료 코드 1.
- 도구가 `data/golden/`에 **쓰지 않음**(임시 root에서 실행 후 `data/golden/` 아래 파일 목록·내용 불변).
- `make generate` 연결: 진행 중 → 검증 미실행, 수거 완료 → 검증·지표·보고서 실행(가짜 클라이언트).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.synth_review sample; echo "exit=$?"   # 생성 전이므로 통과 문서 부족 안내와 exit=1이 정상
grep -n "review-sample\|review-score\|^generate:" Makefile
grep -n "synthetic_review" docs/ARCHITECTURE.md AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 도구가 `data/golden/`을 읽기만 하는가(AGENTS.md CRITICAL)?
   - 표본 수량 기준(유형마다 ≥3, 권한마다 ≥4, 합계 20)을 정확히 세는가(FP-043)?
   - 형식 위반 행 하나가 집계 전체를 멈추지 않는가(FP-030)?
   - `make generate`가 진행 중일 때 검증을 돌려 실패로 끝나지 않는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `data/golden/` 아래에 파일을 만들거나 수정하지 마라(빈 디렉토리·README 포함). 이유: AGENTS.md CRITICAL — golden은 사람이 확정한 기준이다. 결과 디렉토리는 사용자가 처음 저장할 때 만든다(도구는 없으면 안내만).
- 검수 결과를 LLM이나 규칙으로 자동 채우지 마라. 이유: EVAL.md — 사람 검수가 합성 코퍼스 신뢰성의 근거다.
- `make generate`·`make generate-pilot`을 실행하거나 Anthropic API를 호출하지 마라. 이유: ADR-014, FP-040.
- `synth_report.py`의 7절 이외 렌더링, `validate.py`·`generate.py`의 검증·제출 규칙을 바꾸지 마라(연결에 필요한 옵션·종료 코드 추가만 허용). 이유: 한 step 한 모듈, 앞 step 규칙 보존.
- 기존 테스트를 깨뜨리지 마라.
