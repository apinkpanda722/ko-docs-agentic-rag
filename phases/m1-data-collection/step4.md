# Step 4: version-matching

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 명령어 절, manifest 관련 CRITICAL
- `docs/ADR.md` — ADR-022(개정 날짜는 법제처 연혁 기준, 매칭 신뢰도, 실패 파일 제외)
- `docs/EVAL.md` — 최신성 문항은 매칭 신뢰도 정확·근사 버전만 쓴다
- step 1 산출물: `src/kodocs/ingest/manifest.py`
- step 2 산출물: `src/kodocs/ingest/law_api.py` — `data/manifest/law_api.jsonl` 필드(`rule_id`, `serial`, `name`, `issued_date`, `effective_date`, `is_current`)와 정제 JSON의 `name_without_agency`
- step 3 산출물: `src/kodocs/ingest/alio.py` — `data/manifest/alio.jsonl` 필드(`rule_no`, `rule_title`, `rule_last_revised`, `file_no`, `file_name`, `file_kind`, `filename_date`, `parent_file_no`)
- `tests/ingest/`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

ALIO 파일명의 날짜는 제각각이라 버전을 확정할 수 없다. 법제처 연혁은 버전별 발령일자가 구조화되어 있다.
이 step은 **네트워크 없이** 두 manifest만 읽어 ALIO 파일 하나하나를 법제처 버전(`serial`)에 짝짓고,
근거와 신뢰도를 기록한다. 매칭 실패 파일은 파싱 평가 대상에서 제외 표시한다(ADR-022).

## 작업

TDD: `tests/ingest/test_matching.py`를 먼저 작성한다. 입력은 테스트 안에서 만든 가짜 manifest 레코드다.

### 1. `src/kodocs/ingest/matching.py`

```python
class Confidence(StrEnum):
    EXACT = "exact"            # 정확
    APPROXIMATE = "approximate" # 근사
    FAILED = "failed"          # 실패

@dataclass(frozen=True)
class VersionMatch:
    alio_file_no: str
    alio_local_path: str
    alio_sha256: str
    rule_no: str                 # ALIO 규정 번호
    law_rule_id: str | None      # 법제처 행정규칙ID
    law_serial: str | None       # 법제처 행정규칙일련번호 (버전)
    law_issued_date: str | None
    law_is_current: bool | None
    confidence: Confidence
    basis: str                   # 사람이 읽을 근거. 예: "filename day 2024-10-24 == 발령일자"
    candidates: tuple[str, ...]  # 후보 serial (모호할 때)
    parse_eval_eligible: bool    # EXACT/APPROXIMATE → True, FAILED → False

def normalize_rule_name(name: str) -> str: ...
def match_rules(alio_records, law_records) -> dict[str, str | None]: ...   # rule_no → law rule_id
def match_file(alio_record, law_versions_of_rule, *, is_latest_file: bool, approx_days: int = 7) -> VersionMatch: ...
def run(root: Path) -> MatchSummary: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.ingest.matching
```

### 2. 규정 단위 매칭 (ALIO 규정 ↔ 법제처 규정)

- `normalize_rule_name`: 앞의 `(한국지능정보사회진흥원)` 괄호 접두어와 `한국지능정보사회진흥원` 접두어를 떼고, 모든 공백과 `·`/`ㆍ`/`・` 차이를 없앤다.
  예: `"(한국지능정보사회진흥원) 공직자의 이해충돌 방지제도 운영지침"`과 `"한국지능정보사회진흥원 공직자의 이해충돌 방지제도 운영지침"`은 같다.
- 정규화 이름이 **정확히 하나**의 법제처 `rule_id`와 같을 때만 매칭한다. 0개·2개 이상이면 그 규정의 모든 파일은 FAILED(`basis`에 이유).
- 퍼지 문자열 유사도로 규정을 짝짓지 마라. 이유: 잘못 짝지은 규정은 평가 정답을 오염시킨다. 실패는 목록으로 드러나 사람이 확인한다.

### 3. 파일 단위 매칭 (ALIO 파일 ↔ 법제처 버전)

대상: `file_kind`가 `hwp5` 또는 `hwpx`인 파일(zip 멤버 포함). zip 취합본 자체와 pdf·unknown은 매칭 대상이 아니다(출력에서 제외).
날짜 비교에는 각 버전의 `issued_date`(발령일자)와 `effective_date`(시행일자)를 모두 쓴다. 규칙은 위에서부터 적용한다:

| 조건 | 결과 |
|---|---|
| 파일명 날짜 정밀도 day, 발령일자 또는 시행일자와 같은 버전이 **정확히 하나** | EXACT |
| day, 같은 날짜 버전은 없고 ±`approx_days`일 안에 버전이 **정확히 하나** | APPROXIMATE |
| month, 그 달에 발령된 버전이 **정확히 하나** | APPROXIMATE |
| 파일명 날짜 없음(none)이고 그 규정의 **가장 최근 파일**이며, ALIO `rule_last_revised`가 어떤 버전의 발령일자·시행일자와 같음 | APPROXIMATE (`basis`에 "retryRvsnYmd") |
| 후보가 2개 이상 | FAILED, `candidates`에 후보 serial |
| 그 외 (year, range, 후보 0개) | FAILED |

- "가장 최근 파일"은 같은 규정에서 직접 받은 파일(zip 멤버 제외) 중 `file_no`가 가장 큰 파일이다.
- 한 법제처 버전에 ALIO 파일 여러 개가 매칭되면 모두 기록하되, `basis`에 중복을 표시한다(예: 같은 버전의 hwp와 hwpx). 어느 하나를 지우지 마라.
- zip 멤버는 부모 zip 이름의 range가 아니라 **멤버 파일명의 날짜**로 판정한다.

### 4. 출력

- `data/manifest/version_matches.jsonl` — ALIO 파일마다 한 줄(`VersionMatch`를 dict로 + `matched_at`). 실행할 때마다 **새로 쓴다**(append 아님). 이유: 입력 manifest에서 결정론적으로 다시 만들 수 있는 산출물이다.
  step 1 `append_record`를 쓰려면 `required=`로 이 파일의 필수 필드를 넘긴다.
- 출력 줄은 `(rule_no, alio_file_no, zip 멤버 경로)` 순으로 정렬해 diff가 안정적이게 한다.
- 요약: 신뢰도별 파일 수, 규정 매칭 실패 목록(ALIO 제목), 법제처 버전 중 어떤 ALIO 파일과도 짝지어지지 않은 버전 수, 현행 버전과 짝지어진 규정 수.
- 입력 manifest가 없으면 어떤 명령을 먼저 실행해야 하는지(`make collect-law`, `make collect-alio`) 안내하고 0이 아닌 코드로 끝난다.

### 5. CLI와 Make 타깃

- `python -m kodocs.ingest.matching [--root .] [--approx-days 7]`
- `Makefile`에 `match-versions` 타깃을 추가하고 `.PHONY`에 넣는다. `verify` 타깃은 건드리지 않는다.
- `AGENTS.md` 명령어 절에 `make match-versions # ALIO 파일 ↔ 법제처 버전 발령일자 매칭 (네트워크 없음)`을 추가한다.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.ingest.matching --help
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 네트워크 요청이 전혀 없는가? (manifest와 로컬 파일만 읽는다)
   - 표의 모든 규칙과 모호성(후보 2개 이상) 처리가 테스트로 고정되었는가?
   - 추가 의존성이 없는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 후보가 여럿일 때 하나를 고르지 마라(가장 가까운 날짜 등). 이유: 근거 없는 확정은 최신성 평가 정답을 오염시킨다. FAILED로 두고 후보를 남긴다.
- 문자열 유사도·LLM으로 규정 이름을 짝짓지 마라. 이유: 결정론·재현성(ADR 철학).
- `data/manifest/law_api.jsonl`·`alio.jsonl`을 수정하지 마라. 이유: 수집 기록이다. 매칭 결과는 별도 파일에 쓴다.
- 파일 내용을 열어 날짜를 추출하지 마라. 이유: 이 step은 메타데이터 매칭이다. 본문 대조는 M2 범위다.
- 기존 테스트를 깨뜨리지 마라.
