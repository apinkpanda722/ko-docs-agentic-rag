# Step 5: hwp-convert

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 명령어 절, `data/raw/` 규칙
- `docs/ADR.md` — ADR-008(HWP·HWPX → LibreOffice + H2Orestart → PDF, 시그니처로 형식 판정), ADR-014
- step 1 산출물: `src/kodocs/ingest/manifest.py`, `src/kodocs/ingest/filetype.py`
- step 3 산출물: `src/kodocs/ingest/alio.py` — `data/manifest/alio.jsonl` 필드(`local_path`, `sha256`, `file_kind`, `rule_no`, `file_no`)
- `tests/ingest/`, `Makefile`, `pyproject.toml`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

ALIO에서 받은 HWP 5.x·HWPX 파일을 PDF로 바꾼다. 도구는 사용자 PC에 설치된 로컬 의존성이다.
사용자가 확인한 사실(그대로 반영한다):

- LibreOffice 26.8 + H2Orestart 0.7.14 조합으로 HWP 5.x와 HWPX 모두 PDF 변환에 성공했다. 변환 명령:
  `soffice --headless --infilter="Hwp2002_File" --convert-to pdf:writer_pdf_Export --outdir <dir> <file>`
- H2Orestart 확장 식별자는 `ebandal.libreoffice.H2Orestart`다. `unopkg list` 출력에서 이 식별자와 버전으로 설치를 확인할 수 있다.
- macOS에서는 `soffice`가 PATH에 없고 `/Applications/LibreOffice.app/Contents/MacOS/soffice`에 있다(`unopkg`도 같은 폴더).
- **이 Mac에서는 CLI `unopkg add`로 설치하면 Java 구성 요소 등록이 실패했고, 확장 관리자(GUI)로만 성공했다.**
  **Java는 LibreOffice 설정 > 고급에서 JDK 경로를 지정해야 했다.** (H2Orestart는 Java 확장이다)
- **입력 이름이 확장자만 다르면(`a.hwp`, `a.hwpx`) soffice 출력 PDF(`a.pdf`)가 서로 덮어쓴다.**
- 형식은 확장자가 아니라 파일 시그니처로 판정한다. step 1 `sniff`를 쓴다: `HWP5`는 OLE이면서 `FileHeader` 스트림이 `HWP Document File`로 시작, `HWPX`는 `mimetype`이 `application/hwp+zip`인 zip.
- **H2Orestart는 HWP 3.x를 지원하지 않는다.** `HWP3`·`OLE_OTHER`(구형 .doc/.xls 등)는 변환 대상이 아니다(`CONVERTIBLE_KINDS`에 넣지 마라). 요약에 형식별 제외 수로 드러낸다.

## 작업

TDD: `tests/ingest/test_convert.py`를 먼저 작성한다. **soffice·unopkg를 실제로 실행하지 않는다.** subprocess 실행 함수를 주입해
가짜 runner가 출력 폴더에 가짜 PDF(작은 유효 PDF 바이트)를 쓰게 한다.

### 1. 의존성

`uv add pypdf`로 `pypdf`를 추가한다(PDF 페이지 수 기록용. 기술 스택에 이미 있는 파서다). 다른 의존성은 추가하지 마라.

### 2. `src/kodocs/ingest/convert.py`

```python
H2ORESTART_ID = "ebandal.libreoffice.H2Orestart"
MACOS_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
CONVERTIBLE_KINDS = frozenset({FileKind.HWP5, FileKind.HWPX})

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]   # (args, timeout_s) → 결과

@dataclass(frozen=True)
class Toolchain:
    soffice: Path
    unopkg: Path
    soffice_version: str       # `soffice --version` 첫 줄
    h2orestart_version: str

class ToolchainError(RuntimeError):
    """메시지 = 무엇이 없는지 + INSTALL_GUIDE."""

INSTALL_GUIDE: str   # 아래 순서 그대로

def find_toolchain(*, soffice: Path | None = None, runner: Runner = ..., which=shutil.which) -> Toolchain: ...
def output_pdf_path(root: Path, record: Mapping) -> Path: ...
def convert_file(src: Path, out_pdf: Path, toolchain: Toolchain, *, runner: Runner, timeout_s: float = 180) -> None: ...
def run(root: Path, toolchain: Toolchain, *, runner: Runner = ..., only_eligible: bool = False) -> ConvertSummary: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.ingest.convert [--check-only] [--soffice PATH] [--only-eligible]
```

### 3. 도구 점검 (실행 시 가장 먼저)

- soffice 탐색 순서: `--soffice` 인자 → `which("soffice")` → `which("libreoffice")` → `MACOS_SOFFICE`. `unopkg`는 soffice와 같은 폴더 → `which("unopkg")`.
- H2Orestart: `unopkg list` 출력에 `H2ORESTART_ID`가 있는지 보고, 같은 블록의 `Version:` 값을 읽는다.
  `unopkg list`가 실패하면(LibreOffice 창이 열려 있으면 잠길 수 있다) "LibreOffice 창을 모두 닫고 다시 실행"을 안내한다.
- 하나라도 없으면 `ToolchainError`. CLI는 안내문을 출력하고 **종료 코드 2로 멈춘다.** 변환을 시도하지 않는다.
- `INSTALL_GUIDE`는 **이 순서로** 쓴다:
  1. LibreOffice 설치 (https://www.libreoffice.org/download/)
  2. JDK 설치 (H2Orestart는 Java 확장이라 JRE/JDK가 필요하다)
  3. H2Orestart `.oxt` 다운로드: https://github.com/ebandal/H2Orestart/releases (또는 https://extensions.libreoffice.org/en/extensions/show/27504)
  4. LibreOffice를 열고 **도구 > 확장 관리자 > 추가**로 `.oxt`를 설치한다. **CLI `unopkg add`는 macOS에서 Java 구성 요소 등록이 실패할 수 있으니 쓰지 않는다.**
  5. **LibreOffice 설정(macOS: LibreOffice > 설정) > 고급 > Java 옵션**에서 설치한 JDK를 선택하거나 경로를 추가한다.
  6. LibreOffice를 재시작한 뒤, 창을 모두 닫고 `make convert-hwp`를 다시 실행한다.
- `--check-only`는 점검 결과(soffice 경로·버전, H2Orestart 버전)만 출력하고 끝난다.

### 4. 변환

- 입력: `data/manifest/alio.jsonl`의 레코드 중 로컬 파일을 **다시 `sniff`해서** `HWP5`/`HWPX`인 것. manifest의 `file_kind`만 믿지 마라.
  `--only-eligible`이면 `data/manifest/version_matches.jsonl`에서 `parse_eval_eligible=true`인 파일만 변환한다.
- **출력 파일명에 원본 확장자와 해시를 넣는다**: `data/raw/pdf/{rule_no}/{원본파일명(확장자 포함)}.{sha256 앞 12자}.pdf`
  예: `사택 운영지침(2024년도 11월 개정).hwp.3fa1c2d4e5f6.pdf`. 원본 파일명은 step 3과 같은 규칙으로 안전하게 바꾼다.
- soffice는 `--outdir`에 `<입력 stem>.pdf`를 쓰므로, **파일마다 새 임시 폴더를 `--outdir`로 주고** 결과를 최종 경로로 옮긴다.
  같은 폴더를 공유하지 마라. 이유: 확장자만 다른 입력이 서로 덮어쓴다.
- soffice 명령은 위 "배경"의 변환 명령을 그대로 쓴다(`--infilter="Hwp2002_File"`). 인자는 리스트로 넘기고 셸을 쓰지 않는다.
- 종료 코드가 0이어도 출력 PDF가 없거나 `sniff`가 PDF가 아니면 실패다(soffice가 이미 떠 있으면 조용히 아무것도 안 할 수 있다). 실패 메시지에 "LibreOffice 창을 모두 닫고 재시도"를 넣는다.
- 파일별 타임아웃(기본 180초)은 실패로 기록하고 다음 파일로 넘어간다.
- 페이지 수는 `pypdf`로 센다.

### 5. manifest

- `data/manifest/conversions.jsonl` — 변환마다 한 줄: `source_path`, `source_sha256`, `source_kind`, `rule_no`, `file_no`, `output_path`,
  `output_sha256`, `page_count`, `status`(`ok`/`failed`), `error`, `soffice_version`, `h2orestart_version`, `converted_at`.
  step 1 `append_record`를 `required=`로 쓴다. 경로는 저장소 상대 경로.
- 멱등성: 같은 `source_sha256`의 `ok` 레코드가 있고 출력 PDF가 있으며 sha256이 같으면 건너뛴다. `failed`는 재실행 시 다시 시도한다.
- 요약: 변환 성공·실패·건너뜀 수, 총 페이지 수.

### 6. Make 타깃

- `Makefile`에 `convert-hwp` 타깃(`uv run python -m kodocs.ingest.convert`)을 추가하고 `.PHONY`에 넣는다. `verify`는 건드리지 않는다.
- `AGENTS.md` 명령어 절에 `make convert-hwp    # HWP/HWPX → PDF (LibreOffice + H2Orestart 로컬 도구 필요, 실행 시 점검), 사용자 실행`을 추가한다.

### 7. 테스트가 확인할 것

- soffice를 못 찾으면 / H2Orestart가 `unopkg list`에 없으면 `ToolchainError`, 메시지에 안내 6단계가 **순서대로** 들어 있고
  "확장 관리자"와 "고급"이 있으며, CLI 종료 코드가 2이고 runner에 변환 명령이 한 번도 전달되지 않는다.
- macOS 경로 fallback, `--soffice` 우선.
- **`a.hwp`(HWP 5.x, `tests/ingest/fakes.py`의 `hwp5_bytes()`)와 `a.hwpx`(HWPX zip) 두 입력이 서로 다른 PDF 두 개를 만든다**(덮어쓰기 없음). 파일명에 원본 확장자와 해시가 있다.
- `ole_other_bytes()`·`HWP3_HEADER` 파일은 runner를 부르지 않고 형식별 제외로 집계된다.
- 확장자가 `.hwp`인데 내용이 zip이 아닌 HTML이면 변환하지 않는다(시그니처 판정).
- runner가 0을 돌려줘도 PDF가 없으면 실패 기록.
- 타임아웃 실패 기록 후 다음 파일 진행.
- 멱등성, 페이지 수 기록.
- runner에 넘기는 인자에 `--infilter=Hwp2002_File`(또는 동등 표기)과 `pdf:writer_pdf_Export`가 있다.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.ingest.convert --help
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - soffice·unopkg를 실제로 실행하는 테스트가 없는가?
   - 형식 판정이 시그니처 기반인가? 출력 파일명 충돌이 테스트로 막혀 있는가?
   - 추가 의존성은 `pypdf` 하나인가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make convert-hwp`, `--check-only`, soffice, unopkg를 실제로 실행하지 마라. 이유: 사용자 로컬 도구이고, 샌드박스에서 사용자 프로필을 건드릴 수 있다.
- 설치를 자동으로 시도하지 마라(`unopkg add`, brew 등). 이유: 이 Mac에서 CLI 설치는 Java 등록이 실패했고, 설치는 사용자 작업이다. 안내만 한다.
- `-env:UserInstallation`으로 새 LibreOffice 프로필을 지정하지 마라. 이유: H2Orestart는 사용자 프로필에 설치되어 새 프로필에서는 보이지 않는다.
- `shell=True`나 문자열 명령을 쓰지 마라. 이유: 한글·공백·괄호가 든 파일명.
- 여러 입력을 한 `--outdir`에 동시에 변환하지 마라. 이유: 확장자만 다른 입력의 출력이 덮어쓰인다.
- 파싱(텍스트 추출)·표 추출 코드를 만들지 마라. 이유: M2 범위. `pypdf`는 페이지 수에만 쓴다.
- 기존 테스트를 깨뜨리지 마라.
