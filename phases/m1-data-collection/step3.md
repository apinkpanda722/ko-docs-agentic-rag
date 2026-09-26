# Step 3: alio

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — `data/raw/` 규칙, 명령어 절
- `docs/ADR.md` — ADR-002, ADR-008(시그니처로 형식 판정), ADR-014, ADR-022
- step 1 산출물: `src/kodocs/ingest/http.py`, `src/kodocs/ingest/manifest.py`, `src/kodocs/ingest/filetype.py`
- step 2 산출물: `src/kodocs/ingest/law_api.py` (CLI·manifest·멱등성 패턴을 따른다), `tests/ingest/`
- `Makefile`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

ALIO(공공기관 경영정보 공개시스템)에서 NIA(기관 ID `C0392`) 내부규정 95개의 **원본 파일 전 버전**을 받는다.
이 파일은 M2 파싱 입력이자 실제 개정 이력이다. 아래 엔드포인트는 설계 단계에서 ALIO 페이지의 스크립트를 읽고 실제 조회해 확인했다.
ALIO `robots.txt`는 `Allow: /`다. 공개 사이트이므로 **요청 간격(기본 1초)을 반드시 지킨다.**

### ① 목록
`POST https://www.alio.go.kr/item/itemReportListSusi.json`, JSON 본문
`{"pageNo": 1, "apbaId": "C0392", "apbaType": "A2004", "reportFormRootNo": "21110"}`
응답(발췌):
```json
{"status": "success", "data": {
  "page": {"totalPage": 10, "totalCount": 95, "currPage": 1},
  "result": [{"rnum": 1, "idx": "16538", "title": "사택 운영지침", "idate": "2026.09.11", "stDate": "2026.09.09",
              "apbaId": "C0392", "tableName": "COMM_RULE", "idxName": "RULE_NO"}]}}
```
페이지는 1부터 `totalPage`까지 순회한다. `idx`가 규정 번호(rule_no)다.

### ② 상세
`GET https://www.alio.go.kr/occasional/findRuleDtl.json?seq={idx}`
응답(발췌):
```json
{"status": "success", "data": {"ruleNo": 16538, "title": "사택 운영지침", "retryRvsnYmd": "2026.09.09", "insdRuleDivis": "인사·복무·징계",
  "pname": "한국지능정보사회진흥원",
  "bFiles": "16954|사택 운영지침(15.5.12).hwp,123668|사택 운영지침_20210316.hwp,205729|사택 운영지침(2024년도 11월 개정).hwp,229512|사택 운영지침(2026년도 1월 12일 개정).hwpx,241269|사택 운영지침(2026년도 9월 9일 개정).hwpx"}}
```
다른 예: `"221628|한국지능정보사회진흥원 임직원 행동강령(2016-2024년 개정).zip,221629|…(2025년도 9월 24일 개정).hwp,236962|…(2026년도 5월 29일 개정).hwpx"`
- `bFiles`는 `fileNo|파일명`을 쉼표로 이은 문자열이다. 파일명에 쉼표가 들어갈 수 있으므로, 쉼표로 나눈 조각이 `^\d+\|`로 시작하지 않으면 앞 조각에 다시 붙인다.
- `retryRvsnYmd`는 최종 개정일이다.

### ③ 다운로드
`GET https://www.alio.go.kr/download/rulefiledown.json?fileNo={fileNo}`
- 응답 `Content-Disposition`의 파일명은 따옴표가 이중으로 붙어 깨져 있다(`filename=""….hwp"";`). **파일명은 `bFiles` 값을 쓴다.**
- 실패하면 HTML 오류 페이지가 200으로 올 수 있다. 저장 전 step 1 `sniff`로 `HTML`/`UNKNOWN`이면 실패로 기록하고 파일을 남기지 않는다.

## 작업

TDD: `tests/ingest/test_alio.py`를 먼저 작성한다. 모든 HTTP는 `httpx.MockTransport`, 파일은 `tmp_path`에 만든 가짜 바이트(OLE 헤더, HWPX zip, zip 취합본)다.

### 1. `src/kodocs/ingest/alio.py`

```python
ALIO_BASE_URL = "https://www.alio.go.kr"
NIA_APBA_ID = "C0392"
RULE_REPORT_FORM_ROOT_NO = "21110"

@dataclass(frozen=True)
class AlioFileRef:
    file_no: str
    file_name: str          # bFiles 원래 이름

@dataclass(frozen=True)
class AlioRule:
    rule_no: str
    title: str
    category: str | None     # insdRuleDivis
    last_revised: str | None # retryRvsnYmd → YYYY-MM-DD
    files: tuple[AlioFileRef, ...]

@dataclass(frozen=True)
class FilenameDate:
    date: str | None         # YYYY-MM-DD (day) / YYYY-MM (month) / YYYY (year) / None
    precision: str           # "day" | "month" | "year" | "range" | "none"
    range_years: tuple[int, int] | None
    raw: str | None          # 매칭에 쓴 원문 조각

def parse_bfiles(value: str) -> tuple[AlioFileRef, ...]: ...
def parse_filename_date(file_name: str) -> FilenameDate: ...

class AlioClient:
    def __init__(self, http: RateLimitedClient, *, apba_id: str = NIA_APBA_ID) -> None: ...
    def list_rule_nos(self) -> list[tuple[str, str]]: ...   # (rule_no, title)
    def fetch_rule(self, rule_no: str) -> AlioRule: ...
    def download(self, file_no: str) -> bytes: ...

def extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]: ...
def collect(root: Path, client: AlioClient) -> CollectSummary: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.ingest.alio
```

`parse_filename_date`가 처리해야 할 표기(테스트로 고정):
| 파일명 조각 | 결과 |
|---|---|
| `(2026년도 9월 9일 개정)`, `(2026년 9월 9일)` | `2026-09-09`, day |
| `_20210316`, `(20210316)` | `2021-03-16`, day |
| `(15.5.12)` (연 2자리) | `2015-05-12`, day |
| `2024.11.5` | `2024-11-05`, day |
| `(2024년도 11월 개정)` | `2024-11`, month |
| `(2016-2024년 개정)` | range, `(2016, 2024)` |
| 날짜 없음 | none |
유효하지 않은 날짜(13월 등)는 none으로 둔다. 추측으로 채우지 않는다. 이 값은 참고용이고 버전 확정은 step 4가 법제처 연혁과 대조해 한다.

### 2. zip 취합본 압축 해제

- `sniff`가 `ZIP`(HWPX가 아닌 zip)인 파일만 푼다. HWPX는 zip이지만 풀지 않는다.
- 멤버 파일명 디코딩: zip의 UTF-8 플래그(0x800)가 없으면 cp437로 읽힌 이름을 cp437 바이트로 되돌려 **cp949로 디코딩**한다. 실패하면 원래 이름을 쓴다.
- **zip slip 방지**: 절대 경로, `..`, 드라이브 문자를 포함한 멤버는 건너뛰고 경고로 기록한다. 모든 결과 경로가 `dest_dir` 안에 있는지 확인한다.
- 디렉토리 항목과 `__MACOSX/`·`.DS_Store`는 건너뛴다. 중첩 zip은 풀지 않고 멤버로만 기록한다.
- 푼 멤버도 `sniff`로 형식을 기록한다.

### 3. 저장 위치와 manifest

- 원본: `data/raw/alio/{rule_no}/{file_no}__{안전한 파일명}` (파일명의 `/`, `\`, 제어문자만 `_`로 바꾸고 한글은 유지)
- zip 멤버: `data/raw/alio/{rule_no}/{file_no}__zip/{멤버 경로}`
- manifest: `data/manifest/alio.jsonl` — 파일마다 한 줄(zip 멤버 포함). step 1 필수 필드 +
  `rule_no`, `rule_title`, `rule_category`, `rule_last_revised`, `file_no`, `file_name`, `file_kind`(sniff 결과),
  `filename_date`(`FilenameDate`를 dict로), `parent_file_no`(zip 멤버일 때), `zip_member_path`(zip 멤버일 때).
  `source`는 `"alio"`(직접 받은 파일) / `"alio_zip_member"`, `source_url`은 다운로드 URL(zip 멤버는 부모 zip URL),
  `license_note`는 `LICENSE_NOTE_NO_KOGL`.
- 다운로드 실패(HTML·UNKNOWN·HTTP 오류)는 `data/manifest/alio_failures.jsonl`에 `rule_no`, `file_no`, `file_name`, `reason`, `collected_at`으로 남기고 다음 파일로 넘어간다.
- 멱등성: manifest에 같은 `file_no`(zip 멤버는 `file_no`+`zip_member_path`)가 있고 로컬 sha256이 같으면 다시 받지 않는다.
- 실행 끝에 요약(규정 수, 파일 수, 형식별 개수, zip 멤버 수, 실패 수)을 찍는다.

### 4. CLI와 Make 타깃

- `python -m kodocs.ingest.alio [--root .] [--min-interval 1.0] [--limit-rules N]` (`--limit-rules`는 사용자가 소량 시험할 때 쓴다)
- `min_interval`을 0.5초 미만으로 주면 거부한다. 이유: 공개 사이트 부하 방지.
- `Makefile`에 `collect-alio` 타깃을 추가하고 `.PHONY`에 넣는다. `verify` 타깃은 건드리지 않는다.
- `AGENTS.md` 명령어 절에 `make collect-alio   # ALIO 내부규정 원본 파일 수집(전 버전, zip 해제) — 네트워크 호출, 사용자 실행`을 추가한다.

### 5. 테스트가 확인할 것

- 목록 페이지 순회, 상세 파싱, 쉼표가 든 파일명의 `bFiles` 파싱, `parse_filename_date` 표 전체.
- 다운로드가 HTML이면 파일을 남기지 않고 실패 manifest에 기록.
- 확장자가 `.hwp`인데 내용이 HWPX zip이면 `file_kind="hwpx"`로 기록하고 **풀지 않는다**. 일반 zip은 푼다.
- cp949 멤버 이름 복원, zip slip 멤버(`../evil.hwp`, 절대 경로) 차단.
- 멱등성: 두 번째 실행에서 다운로드 요청이 나가지 않는다.
- 요청 간격이 `RateLimitedClient`를 통해 지켜진다(주입 clock).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.ingest.alio --help
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `--help`는 네트워크 없이 끝나야 한다.
2. 아키텍처 체크리스트를 확인한다:
   - step 1 `RateLimitedClient`·`manifest`·`filetype`을 재사용하는가? 형식 판정에 확장자를 쓰지 않았는가?
   - 추가 의존성이 없는가?
   - AGENTS.md CRITICAL을 지켰는가? (원문 비커밋, 네트워크 호출 없음)
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make collect-alio`나 CLI를 실제로 실행하지 마라. alio.go.kr에 어떤 요청도 보내지 마라. 이유: 실제 수집은 사용자가 실행한다(ADR-014).
- 확장자로 형식을 판정하지 마라. 이유: ADR-008, 확장자와 실제 형식이 다른 파일이 있다.
- `Content-Disposition` 파일명을 쓰지 마라. 이유: 이중 따옴표로 깨져 온다.
- 파일명에서 날짜를 추측해 채우지 마라(예: 월 단위를 1일로 확정). 이유: 버전 확정은 step 4가 법제처 연혁으로 하고 근거·신뢰도를 남긴다.
- 원본 파일 내용을 수정하지 마라(기관명 치환 등). 이유: ADR-002 무가공.
- 법제처 매칭·PDF 변환 코드를 만들지 마라. 이유: step 4·5 범위.
- 기존 테스트를 깨뜨리지 마라.
