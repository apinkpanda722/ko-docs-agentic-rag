"""Convert signature-verified HWP/HWPX sources to local PDF files."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from kodocs.ingest.filetype import FileKind, sniff
from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    repo_relative,
    sha256_file,
)

H2ORESTART_ID = "ebandal.libreoffice.H2Orestart"
MACOS_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
CONVERTIBLE_KINDS = frozenset({FileKind.HWP5, FileKind.HWPX})

Runner = Callable[..., subprocess.CompletedProcess]  # (args, timeout_s, env=None)
FONTCONFIG_RELATIVE_PATH = PurePosixPath("infra/fontconfig/fonts.conf")

INSTALL_GUIDE = """설치 안내:
1. LibreOffice 설치 (https://www.libreoffice.org/download/)
2. JDK 설치 (H2Orestart는 Java 확장이라 JRE/JDK가 필요하다)
3. H2Orestart `.oxt` 다운로드: https://github.com/ebandal/H2Orestart/releases (또는 https://extensions.libreoffice.org/en/extensions/show/27504)
4. LibreOffice를 열고 도구 > 확장 관리자 > 추가로 `.oxt`를 설치한다. CLI `unopkg add`는 macOS에서 Java 구성 요소 등록이 실패할 수 있으니 쓰지 않는다.
5. LibreOffice 설정(macOS: LibreOffice > 설정) > 고급 > Java 옵션에서 설치한 JDK를 선택하거나 경로를 추가한다.
6. LibreOffice를 재시작한 뒤, 창을 모두 닫고 `make convert-hwp`를 다시 실행한다.
7. (선택) 함초롬 글꼴(한컴 다운로드 센터 > 글꼴 > 함초롬체, 개인·기업 무료, 상업적 재배포·수정 금지)을 ~/Library/Fonts에 설치하면 원본 쪽수에 가깝게 변환된다."""

CONVERSION_REQUIRED_FIELDS = frozenset(
    {
        "source_path",
        "source_sha256",
        "source_kind",
        "rule_no",
        "file_no",
        "output_path",
        "output_sha256",
        "page_count",
        "status",
        "error",
        "soffice_version",
        "h2orestart_version",
        "converted_at",
        "embedded_fonts",
        "hangul_distinct",
    }
)

_IDENTIFIER_LINE = re.compile(r"^\s*Identifier:\s*(\S+)\s*$")
_VERSION_LINE = re.compile(r"^\s*Version:\s*(\S+)\s*$")
_CLOSE_LIBREOFFICE_GUIDANCE = "LibreOffice 창을 모두 닫고 재시도하세요."


@dataclass(frozen=True)
class Toolchain:
    """Verified local LibreOffice and H2Orestart installation details."""

    soffice: Path
    unopkg: Path
    soffice_version: str
    h2orestart_version: str


@dataclass(frozen=True)
class ConvertSummary:
    """Counts from one conversion run."""

    success_count: int
    failure_count: int
    skipped_count: int
    total_page_count: int
    excluded_file_kinds: dict[str, int]


class ToolchainError(RuntimeError):
    """A missing or unusable local tool followed by the manual install guide."""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message.rstrip()}\n\n{INSTALL_GUIDE}")


class ConversionError(RuntimeError):
    """A source manifest or individual conversion could not be processed."""


def _run_subprocess(
    args: Sequence[str],
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=timeout_s,
        env=dict(env) if env is not None else None,
    )


# -env:UserInstallation으로 프로필을 격리하지 않는다: H2Orestart는 사용자 프로필에
# 설치돼 있어 격리하면 변환이 전부 실패한다.
def find_toolchain(
    *,
    soffice: Path | None = None,
    runner: Runner = _run_subprocess,
    which: Callable[[str], str | None] = shutil.which,
) -> Toolchain:
    """Locate and verify LibreOffice plus the H2Orestart extension."""
    resolved_soffice = _find_soffice(soffice, which)
    resolved_unopkg = _find_unopkg(resolved_soffice, which)

    try:
        version_result = runner([str(resolved_soffice), "--version"], 30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainError(
            f"soffice 버전을 확인할 수 없습니다: {type(exc).__name__}: {exc}"
        ) from exc
    if version_result.returncode != 0:
        raise ToolchainError(
            "soffice --version 실행에 실패했습니다: "
            f"{_result_error(version_result) or '오류 내용 없음'}"
        )
    version_lines = _result_stdout(version_result).splitlines()
    soffice_version = version_lines[0].strip() if version_lines else ""
    if not soffice_version:
        raise ToolchainError("soffice --version 결과에서 버전을 찾을 수 없습니다.")

    try:
        extension_result = runner([str(resolved_unopkg), "list"], 30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainError(
            "unopkg list를 실행할 수 없습니다. LibreOffice 창을 모두 닫고 다시 "
            f"실행하세요: {type(exc).__name__}: {exc}"
        ) from exc
    if extension_result.returncode != 0:
        raise ToolchainError(
            "unopkg list가 실패했습니다. LibreOffice 창을 모두 닫고 다시 실행하세요: "
            f"{_result_error(extension_result) or '오류 내용 없음'}"
        )

    extension_output = "\n".join(
        part
        for part in (
            _result_stdout(extension_result),
            _result_stderr(extension_result),
        )
        if part
    )
    h2orestart_version = _extension_version(extension_output, H2ORESTART_ID)
    if h2orestart_version is None:
        raise ToolchainError(
            f"H2Orestart 확장({H2ORESTART_ID})과 Version 값을 "
            "unopkg list에서 찾을 수 없습니다."
        )
    return Toolchain(
        soffice=resolved_soffice,
        unopkg=resolved_unopkg,
        soffice_version=soffice_version,
        h2orestart_version=h2orestart_version,
    )


def embedded_font_names(pdf_path: Path) -> list[str]:
    """Sorted base font names used by page resources, without subset prefixes."""
    names: set[str] = set()
    for page in PdfReader(pdf_path).pages:
        resources = page.get("/Resources")
        fonts = resources.get_object().get("/Font") if resources is not None else None
        if fonts is None:
            continue
        for font in fonts.get_object().values():
            base_font = font.get_object().get("/BaseFont")
            if base_font is not None:
                names.add(_strip_subset_prefix(str(base_font).lstrip("/")))
    return sorted(names)


def hangul_distinct_count(text: str) -> int:
    """Number of distinct precomposed Hangul syllables (U+AC00–U+D7A3)."""
    return len({character for character in text if "가" <= character <= "힣"})


def _strip_subset_prefix(name: str) -> str:
    prefix, separator, rest = name.partition("+")
    if separator and len(prefix) == 6 and prefix.isalpha() and prefix.isupper():
        return rest
    return name


def output_pdf_path(root: Path, record: Mapping[object, object]) -> Path:
    """Return the collision-safe PDF path for one ALIO manifest record."""
    rule_no = _required_text(record, "rule_no")
    file_name = _required_text(record, "file_name")
    source_sha256 = _required_text(record, "sha256")
    return (
        root
        / "data/raw/pdf"
        / _safe_file_name(rule_no)
        / f"{_safe_file_name(file_name)}.{source_sha256[:12]}.pdf"
    )


def convert_file(
    src: Path,
    out_pdf: Path,
    toolchain: Toolchain,
    *,
    runner: Runner,
    timeout_s: float = 180,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run one isolated soffice conversion and validate its PDF signature."""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kodocs-hwp-") as temporary:
        outdir = Path(temporary)
        args = [
            str(toolchain.soffice),
            "--headless",
            "--infilter=Hwp2002_File",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(outdir),
            str(src),
        ]
        result = runner(args, timeout_s, env)
        if result.returncode != 0:
            raise ConversionError(
                "soffice 변환이 실패했습니다: "
                f"{_result_error(result) or '오류 내용 없음'}. "
                f"{_CLOSE_LIBREOFFICE_GUIDANCE}"
            )

        # outdir은 파일마다 따로 만들므로 이름을 추측하지 않고 유일한 PDF를 쓴다.
        generated_pdfs = sorted(outdir.glob("*.pdf"))
        if not generated_pdfs:
            raise ConversionError(
                "soffice가 출력 PDF를 만들지 않았습니다(PDF 없음). "
                f"{_CLOSE_LIBREOFFICE_GUIDANCE}"
            )
        if len(generated_pdfs) > 1:
            raise ConversionError(
                f"soffice 출력 폴더에 PDF 여러 개({len(generated_pdfs)}개)가 있습니다."
            )
        generated = generated_pdfs[0]
        if sniff(generated) is not FileKind.PDF:
            raise ConversionError(
                "soffice 출력이 유효한 PDF 시그니처가 아닙니다. "
                f"{_CLOSE_LIBREOFFICE_GUIDANCE}"
            )
        shutil.move(str(generated), str(out_pdf))


def run(
    root: Path,
    toolchain: Toolchain,
    *,
    runner: Runner = _run_subprocess,
    only_eligible: bool = False,
) -> ConvertSummary:
    """Convert current ALIO sources and append auditable conversion records."""
    # headless soffice는 FONTCONFIG_FILE 없이 한글 글꼴을 대체하지 못해 한글이 빈칸인
    # PDF를 만든다. 저장소의 대체 규칙(HWP 지정 글꼴 → 함초롬)을 반드시 넘긴다.
    fontconfig_file = root.joinpath(*FONTCONFIG_RELATIVE_PATH.parts)
    if not fontconfig_file.is_file():
        raise ConversionError(
            f"{FONTCONFIG_RELATIVE_PATH}(fonts.conf)이 없습니다. 이 파일 없이 변환하면 "
            "한글이 빠진 PDF가 만들어집니다."
        )
    soffice_env = {**os.environ, "FONTCONFIG_FILE": str(fontconfig_file.resolve())}

    alio_manifest = root / "data/manifest/alio.jsonl"
    if not alio_manifest.is_file():
        raise ConversionError(
            "data/manifest/alio.jsonl이 없습니다. 먼저 make collect-alio를 실행하세요."
        )

    alio_records = latest_records(
        load_records(alio_manifest),
        lambda record: (
            record.get("source"),
            record.get("file_no"),
            record.get("zip_member_path"),
        ),
    )
    if only_eligible:
        eligible_sources = _eligible_sources(root)
        alio_records = [
            record
            for record in alio_records
            if (record.get("local_path"), record.get("sha256")) in eligible_sources
        ]

    conversion_manifest = root / "data/manifest/conversions.jsonl"
    # 출력 경로(원본 파일명 + sha256 앞 12자)가 키다. sha256만 쓰면 내용이 같은
    # 직접 첨부와 zip 멤버가 서로의 기록을 덮어써 매번 다시 변환된다.
    current_conversions = latest_records(
        load_records(conversion_manifest),
        lambda record: record.get("output_path"),
    )
    conversion_by_output = {
        str(record.get("output_path")): record for record in current_conversions
    }

    success_count = 0
    failure_count = 0
    skipped_count = 0
    total_page_count = 0
    excluded_file_kinds: Counter[str] = Counter()

    for source_record in alio_records:
        source_path_text = _required_text(source_record, "local_path")
        source_path = _source_path(root, source_path_text)
        try:
            source_kind = sniff(source_path)
        except OSError as exc:
            failure_count += 1
            _append_conversion(
                conversion_manifest,
                _conversion_record(
                    root=root,
                    source_record=source_record,
                    source_kind=_recorded_kind(source_record),
                    toolchain=toolchain,
                    output_sha256=None,
                    page_count=0,
                    status="failed",
                    error=f"원본 파일을 읽을 수 없습니다: {type(exc).__name__}: {exc}",
                ),
            )
            continue

        if source_kind not in CONVERTIBLE_KINDS:
            excluded_file_kinds[source_kind.value] += 1
            continue

        out_pdf = output_pdf_path(root, source_record)
        output_key = repo_relative(out_pdf, root)
        existing = conversion_by_output.get(output_key)
        if _is_current_success(root, out_pdf, existing):
            skipped_count += 1
            continue

        try:
            convert_file(
                source_path,
                out_pdf,
                toolchain,
                runner=runner,
                env=soffice_env,
            )
            reader = PdfReader(out_pdf)
            page_count = len(reader.pages)
            embedded_fonts = embedded_font_names(out_pdf)
            hangul_distinct = hangul_distinct_count(
                "".join(page.extract_text() or "" for page in reader.pages)
            )
            output_sha256 = sha256_file(out_pdf)
        except subprocess.TimeoutExpired as exc:
            error = (
                f"soffice conversion timeout after {exc.timeout} seconds. "
                f"{_CLOSE_LIBREOFFICE_GUIDANCE}"
            )
        except (ConversionError, OSError, PyPdfError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        else:
            success_count += 1
            total_page_count += page_count
            converted = _conversion_record(
                root=root,
                source_record=source_record,
                source_kind=source_kind.value,
                toolchain=toolchain,
                output_sha256=output_sha256,
                page_count=page_count,
                status="ok",
                error=None,
                embedded_fonts=embedded_fonts,
                hangul_distinct=hangul_distinct,
            )
            _append_conversion(conversion_manifest, converted)
            conversion_by_output[output_key] = converted
            continue

        failure_count += 1
        failed = _conversion_record(
            root=root,
            source_record=source_record,
            source_kind=source_kind.value,
            toolchain=toolchain,
            output_sha256=None,
            page_count=0,
            status="failed",
            error=error,
        )
        _append_conversion(conversion_manifest, failed)
        conversion_by_output[output_key] = failed

    summary = ConvertSummary(
        success_count=success_count,
        failure_count=failure_count,
        skipped_count=skipped_count,
        total_page_count=total_page_count,
        excluded_file_kinds=dict(sorted(excluded_file_kinds.items())),
    )
    _print_summary(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for user-initiated local conversion."""
    parser = argparse.ArgumentParser(
        description="ALIO HWP 5.x/HWPX 원본을 LibreOffice로 PDF 변환합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--soffice", type=Path)
    parser.add_argument("--only-eligible", action="store_true")
    args = parser.parse_args(argv)

    try:
        toolchain = find_toolchain(soffice=args.soffice)
    except ToolchainError as exc:
        print(f"변환 도구 점검 실패: {exc}", file=sys.stderr)
        return 2

    if args.check_only:
        print(f"soffice 경로: {toolchain.soffice}")
        print(f"soffice 버전: {toolchain.soffice_version}")
        print(f"H2Orestart 버전: {toolchain.h2orestart_version}")
        return 0

    try:
        run(
            args.root,
            toolchain,
            only_eligible=args.only_eligible,
        )
    except (ConversionError, OSError, ValueError) as exc:
        print(f"HWP/HWPX 변환 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _find_soffice(requested: Path | None, which: Callable[[str], str | None]) -> Path:
    if requested is not None:
        if requested.is_file():
            return requested
        raise ToolchainError(f"지정한 soffice 파일이 없습니다: {requested}")
    for command in ("soffice", "libreoffice"):
        if found := which(command):
            return Path(found)
    if MACOS_SOFFICE.is_file():
        return MACOS_SOFFICE
    raise ToolchainError(
        "soffice를 찾을 수 없습니다(PATH의 soffice/libreoffice 및 macOS 기본 경로 확인)."
    )


def _find_unopkg(soffice: Path, which: Callable[[str], str | None]) -> Path:
    sibling = soffice.with_name("unopkg")
    if sibling.is_file():
        return sibling
    if found := which("unopkg"):
        return Path(found)
    raise ToolchainError(
        f"unopkg를 찾을 수 없습니다(soffice 폴더 및 PATH 확인): {sibling}"
    )


def _extension_version(output: str, identifier: str) -> str | None:
    current_identifier: str | None = None
    for line in output.splitlines():
        if identifier_match := _IDENTIFIER_LINE.match(line):
            current_identifier = identifier_match.group(1)
            continue
        if (
            version_match := _VERSION_LINE.match(line)
        ) and current_identifier == identifier:
            return version_match.group(1)
    return None


def _result_stdout(result: subprocess.CompletedProcess) -> str:
    return _to_text(result.stdout)


def _result_stderr(result: subprocess.CompletedProcess) -> str:
    return _to_text(result.stderr)


def _result_error(result: subprocess.CompletedProcess) -> str:
    return _result_stderr(result).strip() or _result_stdout(result).strip()


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value if isinstance(value, str) else ""


def _eligible_sources(root: Path) -> set[tuple[object, object]]:
    manifest = root / "data/manifest/version_matches.jsonl"
    if not manifest.is_file():
        raise ConversionError(
            "data/manifest/version_matches.jsonl이 없습니다. "
            "먼저 make match-versions를 실행하세요."
        )
    return {
        (record.get("alio_local_path"), record.get("alio_sha256"))
        for record in load_records(manifest)
        if record.get("parse_eval_eligible") is True
    }


def _source_path(root: Path, relative: str) -> Path:
    manifest_path = PurePosixPath(relative)
    if manifest_path.is_absolute():
        raise ConversionError(f"원본 경로가 저장소 상대 경로가 아닙니다: {relative}")
    candidate = root.joinpath(*manifest_path.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ConversionError(
            f"원본 경로가 저장소 밖을 가리킵니다: {relative}"
        ) from exc
    return candidate


def _is_current_success(
    root: Path,
    expected_output: Path,
    record: Mapping[str, object] | None,
) -> bool:
    if record is None or record.get("status") != "ok":
        return False
    output_path = record.get("output_path")
    output_sha256 = record.get("output_sha256")
    if not isinstance(output_path, str) or not isinstance(output_sha256, str):
        return False
    if output_path != repo_relative(expected_output, root):
        return False
    if not expected_output.is_file():
        return False
    return sha256_file(expected_output) == output_sha256


def _conversion_record(
    *,
    root: Path,
    source_record: Mapping[str, object],
    source_kind: str,
    toolchain: Toolchain,
    output_sha256: str | None,
    page_count: int,
    status: str,
    error: str | None,
    embedded_fonts: list[str] | None = None,
    hangul_distinct: int | None = None,
) -> dict[str, object]:
    return {
        "source_path": _required_text(source_record, "local_path"),
        "source_sha256": _required_text(source_record, "sha256"),
        "source_kind": source_kind,
        "rule_no": _required_text(source_record, "rule_no"),
        "file_no": _required_text(source_record, "file_no"),
        "output_path": repo_relative(output_pdf_path(root, source_record), root),
        "output_sha256": output_sha256,
        "page_count": page_count,
        "status": status,
        "error": error,
        "soffice_version": toolchain.soffice_version,
        "h2orestart_version": toolchain.h2orestart_version,
        "converted_at": now_iso(),
        "embedded_fonts": embedded_fonts if embedded_fonts is not None else [],
        "hangul_distinct": hangul_distinct,
    }


def _append_conversion(path: Path, record: Mapping[str, object]) -> None:
    append_record(path, record, required=CONVERSION_REQUIRED_FIELDS)


def _required_text(record: Mapping[object, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ConversionError(f"ALIO manifest의 {field} 값이 없거나 문자열이 아닙니다.")
    return value


def _recorded_kind(record: Mapping[str, object]) -> str:
    value = record.get("file_kind")
    return value if isinstance(value, str) and value else FileKind.UNKNOWN.value


def _safe_file_name(file_name: str) -> str:
    safe = "".join(
        "_"
        if character in {"/", "\\"} or unicodedata.category(character) == "Cc"
        else character
        for character in file_name
    )
    return safe or "_"


def _print_summary(summary: ConvertSummary) -> None:
    excluded = ", ".join(
        f"{kind} {count}개"
        for kind, count in sorted(summary.excluded_file_kinds.items())
    )
    print(
        f"완료: 변환 성공 {summary.success_count}개, 실패 {summary.failure_count}개, "
        f"건너뜀 {summary.skipped_count}개, 총 {summary.total_page_count}페이지, "
        f"형식별 제외 {excluded or '없음'}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
