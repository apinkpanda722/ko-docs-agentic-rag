# Step 1: ingest-core

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 특히 `data/raw/`·`data/interim/` 커밋 금지, `.env` 금지, `make verify`는 네트워크·API 키 없이 통과
- `docs/ADR.md` — ADR-002(원문 무가공 로컬 사용), ADR-008(시그니처로 형식 판정), ADR-014, ADR-022(두 경로 수집)
- `docs/ARCHITECTURE.md` — `src/kodocs/ingest/`, `data/manifest/`
- `src/kodocs/config.py`, `tests/test_config.py` — 설정 로더와 `.env.example` 동기화 테스트
- `.env.example`, `pyproject.toml`, `Makefile`, `.gitignore`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

M1은 법제처 Open API와 ALIO에서 규정을 수집하고(step 2·3), 버전을 매칭하고(step 4), HWP/HWPX를 PDF로 바꾸고(step 5),
M2 파싱 대상 목록을 만든다(step 6). 이 step은 그 공통 부품 네 가지를 만든다: 설정 키, HTTP 클라이언트, manifest, 파일 형식 판정.
**이 step에서 법제처·ALIO 전용 코드는 만들지 않는다.**

## 작업

모든 항목은 테스트를 먼저 작성한다(TDD). 테스트 파일: `tests/ingest/test_http.py`, `tests/ingest/test_manifest.py`,
`tests/ingest/test_filetype.py` (필요하면 `tests/ingest/__init__.py`), 그리고 기존 `tests/test_config.py` 보강.

### 1. 설정: `LAW_OPEN_API_KEY`

- `.env.example`에 `LAW_OPEN_API_KEY=`를 추가하고 윗줄 주석으로 "법제처 Open API 인증값(OC). 규정 조문·연혁 수집"이라고 적는다.
  기존 형식(주석은 변수 위 줄, 인라인 주석 금지)을 따른다.
- `src/kodocs/config.py`의 `KEY_ENV_VARS`에 `"LAW_OPEN_API_KEY"`를 추가한다. 기존 동기화 테스트가 계속 통과해야 한다.
- 키가 없어도 `load_settings()`는 성공해야 한다(기존 성질 유지).

### 2. 의존성

`uv add httpx`로 런타임 의존성에 `httpx`를 추가한다(`uv.lock` 갱신 포함). 다른 의존성은 추가하지 마라.

### 3. `src/kodocs/ingest/http.py` — 속도 제한·재시도 HTTP 클라이언트

```python
DEFAULT_USER_AGENT: str  # 예: "kodocs-ingest/0.1 (+research; local evaluation)"

class RateLimitedClient:
    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,         # 연속 요청 사이 최소 간격
        max_retries: int = 3,                # 5xx·타임아웃·연결 오류에만 재시도
        backoff_s: float = 2.0,              # 재시도 대기(시도마다 배수 증가)
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,   # 기본 헤더 (Referer 등). User-Agent 기본값 포함
        transport: httpx.BaseTransport | None = None,   # 테스트에서 httpx.MockTransport 주입
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None: ...
    def get(self, url: str, *, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None) -> httpx.Response: ...
    def post_json(self, url: str, payload: Mapping[str, object], *, headers: Mapping[str, str] | None = None) -> httpx.Response: ...
    def close(self) -> None: ...  # 컨텍스트 매니저도 지원
```

핵심 규칙:
- 모든 요청(재시도 포함)은 직전 요청 시작 시점부터 `min_interval_s`가 지나기 전에 나가지 않는다. 주입한 `clock`/`sleep`으로 테스트한다(실제로 잠들지 않는 테스트).
- 4xx는 재시도하지 않는다. 재시도가 모두 실패하면 마지막 원인을 담은 예외를 던진다.
- 예외 메시지·로그에 URL을 넣을 때는 반드시 `sanitize_url()`(아래)을 거친다. 쿼리의 인증값이 새지 않게 한다.
- 호출별 `headers`는 기본 헤더를 덮어쓴다. step 2가 법제처 요청에 `Referer`를 넣는 데 쓴다.

테스트: 간격 보장, 5xx 재시도 후 성공, 4xx 즉시 실패, 재시도 소진 시 예외, 기본 User-Agent·Referer 헤더 전달, 예외 메시지에 `OC=` 값이 없음.

### 4. `src/kodocs/ingest/manifest.py` — manifest 기록

```python
LICENSE_NOTE_NO_KOGL: str = "공공누리 표시 없음: 원문 무가공, 로컬 전용, 재배포 금지"
SECRET_QUERY_PARAMS: frozenset[str] = frozenset({"oc"})   # 대소문자 무시

def sanitize_url(url: str) -> str:
    """쿼리에서 SECRET_QUERY_PARAMS(OC 등)를 제거한 URL. 나머지 파라미터 순서는 유지."""

def sha256_file(path: Path) -> str: ...
def now_iso() -> str:  """KST(+09:00) ISO 8601 초 단위."""

def append_record(manifest_path: Path, record: Mapping[str, object]) -> None:
    """JSONL 한 줄 추가. ensure_ascii=False, sort_keys=True. 필수 필드 누락 시 ValueError."""

def load_records(manifest_path: Path) -> list[dict]: ...   # 파일이 없으면 []

def repo_relative(path: Path, root: Path) -> str:  """manifest에는 저장소 루트 기준 상대 경로(POSIX)만 쓴다."""
```

- `append_record`는 원문 파일 레코드의 필수 필드를 검사한다: `source`, `source_url`, `local_path`, `sha256`, `size_bytes`,
  `collected_at`, `license_note`. `source_url`에 `OC` 같은 비밀 파라미터가 남아 있으면 기록을 거부한다(ValueError).
  `local_path`가 절대 경로면 거부한다.
- 다른 종류의 manifest(step 4 매칭, step 5 변환)는 필드가 다르므로 `append_record(..., required=...)` 처럼 필수 필드 집합을
  인자로 바꿀 수 있게 한다. 기본값은 위 원문 파일 필드다.
- 금지 키 검사: 레코드 어디에든(중첩 포함) `담당자명`·`전화번호` 키가 있으면 거부한다. 이유: AGENTS.md CRITICAL, manifest는 커밋된다.

테스트: `sanitize_url`이 `OC`/`oc`를 지우고 다른 파라미터를 보존, 필수 필드 누락 거부, 비밀 파라미터·절대 경로·금지 키 거부,
한글이 이스케이프 없이 기록, `load_records` 왕복.

### 5. `src/kodocs/ingest/filetype.py` — 파일 시그니처로 형식 판정

```python
class FileKind(StrEnum):
    HWP5 = "hwp5"      # OLE 복합 문서: 시그니처 D0 CF 11 E0 A1 B1 1A E1
    HWPX = "hwpx"      # zip(PK\x03\x04) 이면서 'mimetype' 항목 내용이 'application/hwp+zip'
    ZIP = "zip"        # HWPX가 아닌 zip (ALIO 취합본)
    PDF = "pdf"        # %PDF-
    HTML = "html"      # 앞부분 공백 제거 후 '<' 로 시작 (다운로드 실패 시 오류 페이지)
    UNKNOWN = "unknown"

def sniff(path: Path) -> FileKind: ...
def sniff_bytes(head: bytes, *, zip_path: Path | None = None) -> FileKind: ...  # 구현 재량, 테스트 편의용
```

- **확장자를 판정 근거로 쓰지 마라.** 이유: ALIO에는 확장자와 실제 형식이 다를 수 있고, 확장자만 다른 파일이 섞인다(ADR-008).
- HWPX 판정은 zip을 열어 `mimetype` 항목을 읽는다. 열 수 없는 zip은 `UNKNOWN`.
- 테스트는 `tmp_path`에 바이트로 만든 가짜 파일을 쓴다: OLE 헤더 파일(확장자 `.hwpx`로 저장해도 HWP5), `mimetype`이 있는 zip(확장자 `.hwp`여도 HWPX),
  일반 zip, PDF, HTML, 빈 파일.

## Acceptance Criteria

```bash
make verify
uv run python -c "from kodocs.ingest.manifest import sanitize_url; print(sanitize_url('https://www.law.go.kr/DRF/lawSearch.do?OC=secret&target=pi&type=JSON'))"
# → OC 없이 target=pi&type=JSON 만 남아야 한다
uv run python -c "from kodocs.config import KEY_ENV_VARS; assert 'LAW_OPEN_API_KEY' in KEY_ENV_VARS"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 새 코드가 `src/kodocs/ingest/`에만 있는가?
   - 추가 의존성은 `httpx` 하나인가?
   - AGENTS.md CRITICAL 규칙(키 없이 `make verify` 통과, `.env` 열지 않음, 네트워크 호출 없음)을 지켰는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 테스트에서 실제 네트워크 요청을 하지 마라. 모든 HTTP 테스트는 `httpx.MockTransport`를 쓴다. 이유: `make verify`는 오프라인에서 통과해야 한다.
- 테스트에서 실제로 `sleep` 하지 마라. `clock`/`sleep`을 주입한다. 이유: 속도 제한 테스트가 느려지면 verify가 느려진다.
- 법제처·ALIO URL, 엔드포인트, 응답 파싱 코드를 만들지 마라. 이유: step 2·3의 범위다.
- `.env` 파일을 열거나 읽지 마라. 실제 키 값을 테스트에 쓰지 마라.
- `src/kodocs/retrieval/`·`llm/` 등 다른 패키지를 수정하지 마라.
- `Makefile`의 `verify` 타깃과 `scripts/` 아래 파일을 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
