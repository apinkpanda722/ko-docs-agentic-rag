# Step 7: quality-metrics

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `docs/EVAL.md` — "결과 보고" 절(파일명 날짜, 커밋 해시·모델 버전·고정 조건·측정 환경), "합성 코퍼스 신뢰성"
- `docs/ADR.md` — ADR-001(합성 문서는 실제보다 쉽다), ADR-023(원장·`billing_margin`, 콘솔 대조), **ADR-024**
- `src/kodocs/eval/parsing_report.py`, `src/kodocs/eval/templates/m2_parsing.md`, `tests/eval/test_parsing_report.py` — M2 결과 문서 생성 방식(템플릿 + `<!-- BEGIN generated -->`/`<!-- END generated -->` 영역 갱신, `(실행 후 기록)` 자리표시자). 이 방식을 따른다
- step 3 산출물: `data/synthetic/plan.jsonl`, `canaries.json`, `injections.json`
- step 5 산출물: `data/synthetic/generations/`, `generation_log.jsonl`
- step 6 산출물: `src/kodocs/synth/validate.py`, `data/synthetic/docs/`, `data/manifest/synth_validation.json`
- step 4 산출물: `data/manifest/anthropic_batches.jsonl` 형식, `src/kodocs/costs.py`

## 배경

검증을 통과한 합성 문서의 품질 지표(중복·다양성·분포)를 LLM 없이 계산하고, M3 결과 문서를 만든다. 실제 생성 전에는 수치 자리에 `(실행 후 기록)`이 들어간다. 결과 문서는 EVAL.md 결과 보고 규칙을 따른다(FP-044).

## 작업

TDD: `tests/eval/test_synth_metrics.py`, `tests/eval/test_synth_report.py`를 먼저 작성한다.

### 1. `src/kodocs/eval/synth_metrics.py`

```python
METRICS_PATH = Path("data/manifest/synth_metrics.json")   # 커밋, 결정론
SHINGLE_N = 5                 # 글자 5-gram (공백 정규화 후)
DUP_THRESHOLD = 0.5           # Jaccard 이 값 이상인 쌍을 "근접 중복"으로 나열
def normalize_for_shingles(text: str) -> str: ...        # NFC, 공백 연속 → 한 칸, 제목 줄·인젝션 구간 제외
def shingles(text: str, n: int = SHINGLE_N) -> frozenset[str]: ...
def jaccard(a, b) -> float: ...
def compute(docs: Sequence[Mapping]) -> dict: ...
def run(root: Path) -> int: ...   # python -m kodocs.eval.synth_metrics
```

지표(모두 **검증을 통과한 문서**만 대상. 분자·분모를 같은 대상 집합에서 계산한다 — FP-036 M2 사례):

- 중복: 모든 문서 쌍(약 2만 쌍, MinHash 없이 정확히 계산)의 5-gram Jaccard → 전체·유형 안·유형 간의 최댓값·p95·평균, `DUP_THRESHOLD` 이상 쌍 목록(doc_id 쌍·값, 상위 20개). 인젝션 구간은 제외(같은 문구 4종이 반복되므로).
- 반복 문장: 문장(`.`·`다.`·줄바꿈 기준 분할) 중 3개 이상 문서에 똑같이 나오는 문장 수와 상위 10개(문장 앞 40자).
- 길이: 유형별 본문 글자 수 평균·표준편차·변동계수·최소·최대, 계획 목표 대비 비율.
- 어휘 다양성: 유형별 distinct-2(글자 bigram 고유 비율, 유형 안 전체 합산 기준).
- 문체: 문장 종결 분포(`합니다`/`했습니다`류, `한다`/`했다`류, `함`/`음`/명사형 종결, 기타) 전체·유형별 비율.
- 분포: 유형·권한·팀별 문서 수(계획 대비 통과 수), 카나리아 포함 문서 수, 인젝션 포함 문서 수.
- 사실 커버리지: 시드 normal 사실 중 통과 문서의 `fact_spans`에 1번 이상 나오는 비율, 사실당 등장 문서 수 분포, 2개 이상 문서에 나오는 사실 수(다중 문서 문항 재료).
- 검증: `synth_validation.json`의 통과율·실패 코드 분포(복사).
- 통과 문서 0건이면 지표를 만들지 않고 종료 코드 1(FP-031).
- 부동소수는 소수 4자리로 반올림해 결정론 JSON으로 쓴다.

### 2. `src/kodocs/eval/synth_report.py` + `src/kodocs/eval/templates/m3_synthetic.md`

```python
def render_tables(root: Path) -> str: ...          # manifest만 읽어 생성 영역 Markdown
def write_report(root: Path, *, run_date: date) -> Path: ...   # docs/results/{run_date}-m3-synthetic.md 생성·갱신
def main(argv=None) -> int: ...   # python -m kodocs.eval.synth_report [--date YYYY-MM-DD]
```

- 입력은 커밋되는 manifest·레지스트리만: `synth_metrics.json`, `synth_validation.json`, `synth_db.json`, `anthropic_batches.jsonl`, `costs.jsonl`(anthropic 서비스만), `plan.jsonl` 요약, `canaries.json`·`injections.json`의 수. 본문 텍스트를 결과 문서에 넣지 않는다(반복 문장은 앞 40자까지만).
- 템플릿 절(고정):
  1. **실행 정보** — 실행일, 커밋 해시(`git rev-parse --short HEAD`, 실패 시 `(알 수 없음)`), 생성 모델·`generation` 기록(temperature `null`과 사유, thinking, effort, max_tokens), 템플릿 해시 목록, 시드 파일 해시, 규정 색인 해시, SQLite `content_hash`, 파이썬·`anthropic` SDK 버전
  2. **고정 조건** — 계획 200건(유형·권한 분포), 인라인 표시 규칙, 검증 규칙 버전, 최대 시도 3회, Batch 사용
  3. **생성·비용** — 배치별 제출 수·결과 유형 수·usage 합, 원장 합계(anthropic 서비스), dry-run 추정 대비 실제 입력 토큰 비율(파일럿 보정 근거)
  4. **검증** — 통과·실패·미생성·소진 수, 실패 코드 분포, 카나리아 30·인젝션 12 확인
  5. **품질 지표** — 중복·반복 문장·길이·distinct-2·문체·분포·사실 커버리지 표
  6. **티켓·결재 DB** — 행 수·권한별 행 수
  7. **사람 검수(20건)** — step 8 집계가 채우는 자리(`(검수 후 기록)`)
  8. **콘솔 대조(FP-037)** — 표: 실행 전 콘솔 Usage(USD·토큰), 실행 후 콘솔 Usage, 증가분, 원장 합계, 차이·차이율. 모두 `(사용자 기록)` 자리표시자. 절차 문구 고정: "① 제출 전 Anthropic 콘솔 Usage/Cost를 기록한다 ② 수거 완료 후 다시 기록한다 ③ 증가분과 원장 합계(`costs.jsonl`의 anthropic 서비스, 부가세 제외 금액으로 환산)를 비교한다 ④ 차이가 있으면 `billing_margin`(현재 1.05) 조정 여부를 ADR-023에 기록한다. errored·canceled·expired는 공식 문서상 미과금이므로 원장에 없고, 콘솔에 과금이 보이면 차이 원인으로 적는다."
  9. **한계** — 합성 문서는 실제보다 쉽다(ADR-001), 텍스트 원본만 있고 문서 형식 다양성 없음(ADR-024), 모델 샘플링 파라미터 미지원으로 동일 요청 재현 불가(캐시로 대체)
- 수치가 없는 자리는 `(실행 후 기록)`. 생성 영역 밖(사람이 쓴 해석)은 갱신 시 보존한다(M2 방식).
- 테스트: manifest가 없을 때 자리표시자로 렌더링, 있을 때 수치 렌더링, 생성 영역 밖 텍스트 보존, 본문 텍스트 미포함, 커밋 해시·모델·temperature null 사유 포함.

### 3. Makefile·AGENTS.md

- `synth-metrics: uv run python -m kodocs.eval.synth_metrics`
- `synth-report: uv run python -m kodocs.eval.synth_report`
- `.PHONY` 추가. AGENTS.md 명령어 절:
  - `make synth-metrics  # 합성 문서 중복·다양성·분포·사실 커버리지 → data/manifest/synth_metrics.json (네트워크 없음)`
  - `make synth-report   # M3 결과 문서 docs/results/{실행일}-m3-synthetic.md 생성·갱신 (네트워크 없음)`
- 이 step 안에서 `make synth-report`를 실행해 자리표시자 버전 결과 문서를 만든다(실제 생성 전).

## Acceptance Criteria

```bash
make verify
make synth-report
ls docs/results/ | grep m3-synthetic
grep -c "(실행 후 기록)" docs/results/*-m3-synthetic.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 결과 문서에 날짜·커밋 해시·모델·생성 설정·고정 조건·도구 버전이 있는가(FP-044)?
   - 비율 지표의 분자·분모가 같은 대상 집합(통과 문서)인가(FP-036 M2 사례)?
   - 콘솔 대조 절차와 표가 있는가(FP-037)?
   - 결과 문서·manifest에 합성 본문 전문이 들어가지 않는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 지표 계산에 LLM·임베딩 모델을 쓰지 마라. 이유: 결정론 우선(ADR 철학), 비용·재현성.
- 결과 문서에 수치를 손으로 채우거나 추정값을 넣지 마라. 실행 전 수치는 `(실행 후 기록)`으로 둔다. 이유: FP-044, 측정 없이 판단하지 않는다.
- 실패 문서·미생성 문서를 지표 분모에 섞지 마라. 이유: FP-036(분자·분모 대상 집합 불일치).
- `src/kodocs/eval/parsing_report.py`와 M2 결과 문서를 수정하지 마라. 공통 함수가 필요하면 새 모듈에 복제하지 말고 import만 한다. 이유: 한 step 한 모듈, M2 결과 보존.
- `data/synthetic/`의 파일을 수정하지 마라(읽기만). 이유: 검증 산출물이 기준이다.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
