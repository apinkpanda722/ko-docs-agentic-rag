# Step 2: law-api

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — `data/raw/`·`data/interim/` 규칙, 담당자 정보·`OC` 비노출 규칙, 명령어 절
- `docs/ADR.md` — ADR-002, ADR-014, ADR-022
- `docs/ARCHITECTURE.md` — `ingest/`, `data/` 트리
- step 1 산출물: `src/kodocs/ingest/http.py`, `src/kodocs/ingest/manifest.py`, `src/kodocs/config.py`, `tests/ingest/`
- `Makefile`, `.gitignore`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

법제처 Open API에서 NIA 규정 95개의 **현행·연혁 버전 본문**을 받는다. 이 데이터는 M2 파싱 정답 텍스트이자 개정 날짜의 기준(ADR-022)이다.
아래 API 사양과 응답 예시는 설계 단계에서 문서 예시용 키로 실제 조회해 확인한 것이다.

### 목록 API
`GET https://www.law.go.kr/DRF/lawSearch.do`
파라미터: `OC`(인증값, 필수), `target=pi`(공공기관 규정), `type=JSON`, `query=한국지능정보사회진흥원`(규정명 검색),
`nw=1`(현행, 기본) 또는 `nw=2`(연혁), `display`(최대 100), `page`(1부터).

응답(발췌):
```json
{"AdmRulSearch": {"resultCode": "00", "resultMsg": "success", "totalCnt": "95", "page": "1", "target": "pi",
  "admrul": [{"현행연혁구분": "현행", "행정규칙명": "(한국지능정보사회진흥원) ICT 기금사업 및 연구개발사업 관리지침",
    "발령일자": "20241024", "시행일자": "20241024", "행정규칙종류": "공단규정", "소관부처명": "한국지능정보사회진흥원",
    "제개정구분코드": "200403", "제개정구분명": "일부개정", "행정규칙ID": "2126297", "행정규칙일련번호": "2200000135767",
    "발령번호": "9999", "행정규칙상세링크": "/DRF/lawService.do?OC=...&target=pi&ID=2200000135767&type=HTML&mobileYn="}]}}
```
- 숫자도 문자열로 온다. 결과가 1건이면 `admrul`이 리스트가 아니라 객체일 수 있다고 가정하고 정규화한다.
- 규정명 검색이라 다른 기관 규정이 섞일 수 있다. **`소관부처명 == "한국지능정보사회진흥원"` 완전 일치로 거른다.**
- `행정규칙ID`는 규정 단위(버전 공통), `행정규칙일련번호`는 버전 단위 키다.
- `행정규칙상세링크`에 `OC`가 들어 있다. 저장·기록 전에 반드시 제거한다.

### 본문 API
`GET https://www.law.go.kr/DRF/lawService.do?OC=...&target=pi&ID={행정규칙일련번호}&type=JSON`

응답 최상위는 `{"AdmRulService": {...}}`이고 키는 `행정규칙기본정보`, `조문내용`, `부칙`, `별표`다(발췌):
```json
{"AdmRulService": {
  "행정규칙기본정보": {"행정규칙명": "...", "행정규칙ID": "2126297", "행정규칙일련번호": "2200000135767", "발령일자": "20241024",
     "제개정구분명": "일부개정", "현행여부": "Y", "소관부처명": "한국지능정보사회진흥원", "조문형식여부": "Y",
     "담당부서기관명": "경영기획실", "담당자명": "(실명)", "전화번호": "(전화번호)", "생성일자": "20260914", "행정규칙종류": "공단규정"},
  "조문내용": ["제1장 총칙", "제1조(목적) 이 지침은 ..."],
  "부칙": {"부칙공포일자": ["20211126", "20241024"], "부칙내용": ["부칙 <제9999호, 2021.11.26.>제1조(시행일) ..."]},
  "별표": {"별표단위": [{"별표키": "000100", "별표번호": "0001", "별표가지번호": "00", "별표구분": "별지",
     "별표제목": "표준협약서", "별표서식파일링크": "/LSW/flDownload.do?flSeq=169223339"}]}}}
```
- `조문내용`·`부칙내용`·`부칙공포일자`는 문자열 하나일 수도, 리스트일 수도 있다. `별표`·`별표단위`는 없거나 객체 하나일 수 있다. 모두 리스트로 정규화한다.
- **`담당자명`·`전화번호`가 들어 있다.** 원본은 `data/raw/`에만 두고 정제 산출물·manifest에 넣지 않는다(AGENTS.md CRITICAL).

### 인증 실패
- 인증값이 틀렸거나 신청 시 등록한 도메인/IP와 요청이 맞지 않으면 "사용자 정보 검증에 실패하였습니다" 류의 메시지가 온다.
  **`Referer` 헤더가 없으면 이 실패가 난다는 사례가 있다.** 응답 형식(JSON/HTML)은 확정되지 않았으므로 둘 다 처리한다.
- 인증값은 사용자가 발급 중이며 `.env`의 `LAW_OPEN_API_KEY`에 넣는다.

## 작업

TDD: `tests/ingest/test_law_api.py`를 먼저 작성한다. 응답 fixture는 위 예시를 바탕으로 **가짜 값**으로 만든다(`tests/ingest/fixtures/`).
fixture의 담당자명·전화번호는 `"테스트담당자"`, `"000-0000-0000"` 같은 명백한 가짜 값을 쓴다.

### 1. `src/kodocs/ingest/law_api.py`

```python
LAW_BASE_URL = "https://www.law.go.kr"
NIA_AGENCY_NAME = "한국지능정보사회진흥원"
PERSONAL_FIELDS: frozenset[str] = frozenset({"담당자명", "전화번호"})

class LawApiError(RuntimeError): ...
class LawApiAuthError(LawApiError):
    """인증 실패. 메시지에 원인 후보를 담는다: 인증값(LAW_OPEN_API_KEY) 확인, 신청 시 등록한 도메인/IP,
    --referer 로 Referer 지정. 인증값 자체는 넣지 않는다."""

@dataclass(frozen=True)
class RuleVersion:
    rule_id: str            # 행정규칙ID
    serial: str             # 행정규칙일련번호
    name: str               # 행정규칙명 (원문 그대로)
    issued_date: str        # 발령일자 YYYY-MM-DD
    effective_date: str | None   # 시행일자 YYYY-MM-DD
    is_current: bool        # 현행연혁구분 == "현행"
    revision_type: str      # 제개정구분명

class LawApiClient:
    def __init__(self, oc: str, http: RateLimitedClient, *, referer: str | None = None) -> None: ...
    def list_versions(self, agency: str = NIA_AGENCY_NAME, *, history: bool) -> list[RuleVersion]:
        """nw=1(현행) 또는 nw=2(연혁) 전 페이지 순회, 소관부처명 완전 일치 필터."""
    def fetch_body(self, serial: str) -> dict: """본문 JSON 원본(dict)."""

def clean_body(raw: dict, version: RuleVersion) -> dict:
    """정제 산출물. PERSONAL_FIELDS를 중첩 어디서든 제거하고, 리스트 정규화, 날짜 YYYY-MM-DD, 링크의 OC 제거."""

def collect(root: Path, client: LawApiClient, *, include_history: bool = True) -> CollectSummary: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.ingest.law_api
```

응답 판정 규칙:
- 본문이 JSON으로 파싱되지 않으면: 텍스트에 "검증" 또는 "인증"이 있으면 `LawApiAuthError`, 아니면 `LawApiError`(상태 코드와 앞 200자, `OC` 제거).
- JSON이지만 인증 실패 문구가 있거나, 목록의 `resultCode`가 `"00"`이 아니면 각각 `LawApiAuthError`/`LawApiError`.
- 인증 실패는 첫 요청에서 드러나면 즉시 전체 수집을 멈춘다(95건을 모두 실패시키지 않는다).

정제 JSON 형식(`clean_body` 반환값, 구현 재량으로 필드 추가 가능):
```json
{"rule_id": "...", "serial": "...", "name": "...", "name_without_agency": "ICT 기금사업 및 연구개발사업 관리지침",
 "kind": "공단규정", "issued_date": "2024-10-24", "effective_date": "2024-10-24", "is_current": true, "revision_type": "일부개정",
 "articles": ["제1장 총칙", "제1조(목적) ..."], "addenda": [{"date": "2021-11-26", "text": "..."}],
 "annexes": [{"key": "000100", "number": "0001", "branch": "00", "kind": "별지", "title": "표준협약서", "file_link": "/LSW/flDownload.do?flSeq=..."}]}
```
`name_without_agency`는 앞의 `(한국지능정보사회진흥원) ` 접두어를 뗀 이름이다(step 4 매칭에 쓴다).

### 2. 저장 위치와 manifest

- 원본 응답: `data/raw/law/{rule_id}/{serial}.json` (응답 바이트 그대로)
- 정제 산출물: `data/interim/law/{rule_id}/{serial}.json`
- manifest: `data/manifest/law_api.jsonl` — 버전마다 한 줄. step 1 `append_record` 필수 필드 +
  `rule_id`, `serial`, `name`, `issued_date`, `effective_date`, `is_current`, `revision_type`, `article_count`, `annex_count`,
  `annex_titles`, `interim_path`, `interim_sha256`. `source="law_api"`, `source_url`은 `OC`를 뺀 본문 URL,
  `license_note`는 `LICENSE_NOTE_NO_KOGL`.
- 멱등성: manifest에 같은 `serial`이 있고 로컬 원본의 sha256이 일치하면 다시 받지 않는다. 중단 후 재실행하면 이어서 받는다.
- 목록 단계 결과(현행·연혁 버전 수, 규정 수)를 표준 출력 요약에 찍는다.
- `.gitignore`에 `data/interim/`을 추가한다.

### 3. CLI와 Make 타깃

- `python -m kodocs.ingest.law_api [--root .] [--no-history] [--referer URL] [--min-interval 1.0]`
- 인증값은 `load_settings().require_key("LAW_OPEN_API_KEY")`로 읽는다. **없으면 네트워크 요청 전에** 안내 메시지를 찍고 0이 아닌 코드로 끝난다.
- `LawApiAuthError`는 스택트레이스 대신 원인 후보 안내를 찍고 종료 코드 2로 끝낸다.
- `Makefile`에 `collect-law` 타깃(`uv run python -m kodocs.ingest.law_api`)을 추가하고 `.PHONY`에 넣는다. `verify` 타깃은 건드리지 않는다.
- `AGENTS.md` 명령어 절에 `make collect-law    # 법제처 Open API 규정 수집(현행·연혁) — 네트워크 호출, 사용자 실행`을 추가한다.

### 4. 테스트가 확인할 것

- 목록 페이지 순회(2페이지 이상), 단일 객체 `admrul` 정규화, 다른 기관 규정 필터링, 현행/연혁 구분.
- `Referer`를 지정하면 모든 요청 헤더에 실린다.
- 인증 실패: HTML 응답·JSON 응답 두 형태 모두 `LawApiAuthError`, 메시지에 원인 후보가 있고 **인증값 문자열이 없다**. 첫 실패에서 수집이 멈춘다.
- **담당자 정보 제거**: 정제 파일과 manifest 줄 어디에도 `담당자명`·`전화번호` 키와 fixture의 가짜 값(`테스트담당자`, `000-0000-0000`)이 없다. 원본 파일에는 남아 있다.
- 정제 산출물·manifest·표준 출력 어디에도 인증값(테스트용 가짜 `OC`)이 없다.
- 리스트 정규화(문자열 하나짜리 `조문내용`, 객체 하나짜리 `별표단위`, `별표` 없음).
- 멱등성: 두 번째 `collect` 실행 시 본문 요청이 나가지 않는다.
- 키가 없을 때 CLI가 네트워크 요청 없이 실패한다.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.ingest.law_api --help
grep -n "data/interim/" .gitignore
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `--help`는 네트워크 없이 끝나야 한다.
2. 아키텍처 체크리스트를 확인한다:
   - 코드가 `src/kodocs/ingest/`에 있고 step 1의 `RateLimitedClient`·`manifest`를 재사용하는가?
   - 추가 의존성이 없는가?
   - AGENTS.md CRITICAL(담당자 정보·`OC` 비노출, `data/raw`·`data/interim` 비커밋, 네트워크 호출 없음)을 지켰는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make collect-law`나 CLI를 실제 인자로 실행하지 마라. law.go.kr에 어떤 요청도 보내지 마라. 이유: 실제 호출은 사용자가 실행한다(ADR-014), 인증키도 아직 없다.
- 실제 응답을 fixture로 내려받지 마라. 위 예시를 바탕으로 가짜 값으로 만든다.
- 담당자명·전화번호를 정제 산출물·manifest·로그에 남기지 마라. 이유: 실명 개인정보이고 manifest는 커밋된다.
- 인증값을 URL·예외 메시지·manifest·표준 출력에 남기지 마라. 이유: 비밀 값이다. URL은 `sanitize_url`을 거친다.
- 별표 서식 파일(`flDownload.do`)을 내려받지 마라. 이유: M1 범위는 별표 **목록**까지다.
- `.env`를 열지 마라. `src/kodocs/retrieval/` 등 다른 패키지를 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
