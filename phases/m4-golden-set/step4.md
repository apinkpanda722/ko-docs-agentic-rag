# Step 4: lexical-overlap

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `docs/ADR.md` — **ADR-025**(어휘 누수: 바이그램 포함률 ≤ 0.6, 최장 공통 부분 문자열 ≤ 12자는 초기값, 포함률 ≤ 0.2면 `lexical_mismatch`)
- `docs/EVAL.md` — 어휘 불일치 태그("새 유형을 만들지 않는다"), 검색 지표의 어휘 불일치 태그 성능
- step 1: `src/kodocs/eval/golden.py`, step 2: `src/kodocs/eval/golden_specs.py`, step 3: `src/kodocs/eval/query_draft.py`(`validate_draft` 검사 함수 목록)
- `data/synthetic/docs/*.json`, `data/synthetic/queries/specs.jsonl`

## 배경

질의가 문서 문장을 그대로 베끼면 BM25가 부당하게 유리해진다. 질의 ↔ 관련도 2 정답 구간 텍스트의 어휘 겹침을 결정론으로 재고, 상한을 넘으면 초안 실패로 돌리며, 겹침이 아주 낮은 문항에 `lexical_mismatch` 태그를 붙인다. 상한은 초기값이고 실측 분포를 보고 사람이 확정한다(FP-016: 경계 규칙은 실제 데이터 전체에 돌려 보고 확정).

## 작업

TDD: `tests/eval/test_lexical.py`를 먼저 작성한다.

### 1. `src/kodocs/eval/lexical.py`

```python
CONTAINMENT_CAP = 0.6       # 초기값. 확정은 ADR-025 변경 기록으로
LCS_CAP = 12
MISMATCH_MAX = 0.2
def normalize(text: str) -> str: ...                 # NFC, 공백·문장부호 제거
def strip_values(text: str, values: Sequence[str]) -> str: ...   # 식별자·날짜·주어 값 제거(정규화 후)
def bigram_containment(query: str, target: str) -> float | None: ...   # 질의 바이그램 중 target에도 있는 비율, 질의 바이그램 0개면 None
def longest_common_substring(query: str, target: str) -> int: ...
def overlap(spec: Question, question: str, docs: Mapping[str, Mapping]) -> dict: ...
    # {containment, lcs, target_chars}: target = 관련도 2 qrel 구간 텍스트를 이은 것.
    # 계산 전 question·target 모두에서 anchor_values와 식별자(extract_identifiers)·날짜 표기를 제거한다.
def summarize(rows: Sequence[dict]) -> dict: ...     # 유형별·전체 분포(n, min, p25, p50, p75, p90, max), 상한 초과 수, mismatch 수
```

- 대상 집합: qrels(관련도 2)가 있는 문항만. unanswerable·sql·permission 문항처럼 비교 대상 구간을 계산할 수 없으면 `containment=None`으로 두고 분포에서 뺀다. **분포의 분자·분모는 같은 문항 집합**에서 구한다(FP-036 M2 사례: 비율 지표의 대상 집합 불일치).

### 2. `query_draft` 검증에 연결

- `validate_draft`의 검사 목록에 `overlap_cap`을 추가: `containment > CONTAINMENT_CAP` 또는 `lcs > LCS_CAP`이면 실패(재시도 대상).
- 검증 통과 문항에 `containment ≤ MISMATCH_MAX`이면 태그 `lexical_mismatch`를 붙여 `validated.jsonl`에 쓴다. 태그는 유형을 바꾸지 않는다.
- 검증 결과 요약 `data/manifest/golden_query_validation.json`(결정론, 시각 없음): `{validated, failed, by_code, overlap: summarize(...), caps: {containment, lcs, mismatch_max}, lexical_mismatch: {n, share}}`.

### 3. 오프라인 재검증 명령

- `query_draft`에 `--validate-only` 옵션: 네트워크·키 없이 이미 수거한 초안(`drafts/`)만 다시 검증하고 `validated.jsonl`·요약을 다시 쓴다(상한을 바꾼 뒤 재판정용).
- Makefile `query-validate: uv run python -m kodocs.eval.query_draft --validate-only`, AGENTS.md: `make query-validate  # 수거한 질의 초안 결정론 재검증(답 누수·어휘 겹침 상한·태그) → data/manifest/golden_query_validation.json (네트워크 없음)`

### 4. 분포 보고 명령(초안 수거 뒤 사용자가 봄)

- `lexical.py`에 `main(argv=None)`(`python -m kodocs.eval.lexical report`): 네트워크·키 없이 `data/synthetic/queries/validated.jsonl`과 수거 캐시(`drafts/`, 실패 초안 포함)를 읽어 유형별 포함률·최장 공통 부분 문자열 분포(n, min, p25, p50, p75, p90, max), 현재 상한에서의 초과 수, 상한 후보별 초과 수(포함률 0.5·0.6·0.7, LCS 10·12·15)와 `lexical_mismatch` 비율을 **표준 출력에만** 표로 출력한다. 파일을 쓰지 않는다(상한 확정을 위한 관찰용).
- 수거한 초안이 하나도 없으면 "수거한 초안 없음, `make queries` 먼저" 안내 후 종료 코드 1(FP-031).
- Makefile `lexical-report: uv run python -m kodocs.eval.lexical report`, AGENTS.md: `make lexical-report  # 수거한 질의 초안의 어휘 겹침 분포·상한 후보별 초과 수 출력(파일 쓰지 않음, 상한 확정용, 네트워크 없음)`

### 5. 결과 템플릿에 상한 상태 표시

- `src/kodocs/eval/templates/m4_golden.md`의 생성 영역 밖 고정 절에 "어휘 겹침 상한" 소절을 추가한다: "바이그램 포함률 ≤ 0.6·최장 공통 부분 문자열 ≤ 12자는 **초기값**이다. `make queries` 뒤 `make lexical-report`의 실측 분포를 보고 사람이 확정하며, 확정값과 근거는 ADR-025 변경 기록에 적는다. 확정 전: (사람 기록 대기)". 생성 영역 마커는 건드리지 않는다.

### 6. 테스트(필수)

- 바이그램 포함률·최장 공통 부분 문자열의 경계값(빈 문자열, 한 글자, 완전 일치, 완전 불일치).
- 주어 값·식별자·날짜를 제거하고 계산함(ID만 같은 질의는 포함률이 부풀지 않음).
- 상한 초과 → `overlap_cap` 실패, 0.2 이하 → `lexical_mismatch` 태그, 0.2 초과 0.6 이하 → 태그 없음.
- `summarize`가 `containment=None` 문항을 분모에서 뺌.
- 같은 초안으로 `--validate-only` 두 번 → 결과 파일 바이트 동일.
- 수거한 초안이 하나도 없으면 안내 후 종료 코드 1(FP-031: 0건을 성공으로 끝내지 않는다).
- `lexical report`가 파일을 쓰지 않음(실행 전후 `data/` 아래 파일 목록·내용 동일), 초안 fixture로 상한 후보별 초과 수를 출력함, 초안이 없으면 종료 코드 1.
- 초안(`validated.jsonl`·`drafts/`)이 저장소에 아직 없어도 `make verify`가 통과한다(테스트는 `tmp_path` fixture만 쓴다).

## Acceptance Criteria

```bash
make verify
make query-validate; echo "exit=$?"   # 수거한 초안이 아직 없으므로 "수거한 초안 없음, `make queries` 먼저" 안내와 exit=1이 정상
make lexical-report; echo "exit=$?"    # 수거한 초안이 없으므로 안내와 exit=1이 정상
grep -n "query-validate\|lexical-report" Makefile AGENTS.md
grep -n "초기값" src/kodocs/eval/templates/m4_golden.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 상한이 상수 한 곳에만 있고 요약에 기록되는가(재현성)?
   - 분포 계산의 분자·분모가 같은 문항 집합인가(FP-036)?
   - `lexical_mismatch`가 새 유형이 아니라 태그인가(EVAL)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 상한을 초안 결과에 맞춰 이 step에서 바꾸지 마라. 이유: 상한 확정은 실측 분포를 본 사람이 ADR-025 변경 기록으로 한다(FP-016).
- 형태소 분석기(nori 등)나 외부 NLP 라이브러리를 새로 의존성에 넣지 마라. 이유: 검색 실험 변수(분석기)와 누수 지표가 섞이지 않게 분석기 중립인 글자 바이그램을 쓴다.
- `data/synthetic/queries/drafts/`의 수거 캐시를 수정하지 마라. 이유: 재현성의 근거다.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
