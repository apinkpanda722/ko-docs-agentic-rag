"""Tests for local HWP/HWPX to PDF conversion orchestration."""

import json
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fakes import HWP3_HEADER, hwp5_bytes, ole_other_bytes
from pypdf import PdfWriter

from kodocs.ingest import convert
from kodocs.ingest.convert import (
    CONVERSION_REQUIRED_FIELDS,
    H2ORESTART_ID,
    INSTALL_GUIDE,
    ConvertSummary,
    Toolchain,
    ToolchainError,
    find_toolchain,
    output_pdf_path,
    run,
)
from kodocs.ingest.manifest import sha256_file

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_FONTS_CONF = PROJECT_ROOT / "infra/fontconfig/fonts.conf"


@pytest.fixture(autouse=True)
def _fontconfig_in_tmp_root(tmp_path: Path) -> None:
    """run()은 저장소 루트의 infra/fontconfig/fonts.conf를 요구한다."""
    target = tmp_path / "infra/fontconfig/fonts.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(REPO_FONTS_CONF.read_bytes())


def _pdf_bytes(page_count: int = 1) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


def _hwpx_bytes(path: Path) -> bytes:
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", "content")
    return path.read_bytes()


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            f"{json.dumps(record, ensure_ascii=False, sort_keys=True)}\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _source_record(
    root: Path,
    relative_path: str,
    *,
    file_no: str,
    file_name: str | None = None,
    file_kind: str = "hwp5",
    source: str = "alio",
    zip_member_path: str | None = None,
) -> dict[str, object]:
    path = root / relative_path
    record: dict[str, object] = {
        "source": source,
        "local_path": relative_path,
        "sha256": sha256_file(path),
        "file_kind": file_kind,
        "rule_no": "10",
        "file_no": file_no,
        "file_name": file_name or path.name,
    }
    if zip_member_path is not None:
        record["zip_member_path"] = zip_member_path
    return record


def _toolchain() -> Toolchain:
    return Toolchain(
        soffice=Path("/fake/soffice"),
        unopkg=Path("/fake/unopkg"),
        soffice_version="LibreOffice 26.8.0.0",
        h2orestart_version="0.7.14",
    )


class ConversionRunner:
    def __init__(
        self,
        *,
        missing_for: frozenset[str] = frozenset(),
        timeout_for: frozenset[str] = frozenset(),
        pages: int = 1,
        output_names: Callable[[Path], list[str]] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.missing_for = missing_for
        self.timeout_for = timeout_for
        self.pages = pages
        self.output_names = output_names or (lambda source: [f"{source.stem}.pdf"])

    def __call__(
        self,
        args: Sequence[str],
        timeout_s: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        call = [str(arg) for arg in args]
        self.calls.append((call, timeout_s))
        self.envs.append(env)
        source = Path(call[-1])
        if source.name in self.timeout_for:
            raise subprocess.TimeoutExpired(call, timeout_s)
        if source.name not in self.missing_for:
            outdir = Path(call[call.index("--outdir") + 1])
            for name in self.output_names(source):
                (outdir / name).write_bytes(_pdf_bytes(self.pages))
        return subprocess.CompletedProcess(call, 0, stdout="", stderr="")


def _assert_install_guide_order(message: str) -> None:
    markers = [
        "LibreOffice 설치",
        "JDK 설치",
        "H2Orestart `.oxt` 다운로드",
        "확장 관리자",
        "고급 > Java 옵션",
        "make convert-hwp",
    ]
    positions = [message.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "unopkg add" in message


def test_find_toolchain_missing_soffice_includes_ordered_install_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(convert, "MACOS_SOFFICE", tmp_path / "missing-soffice")
    called = False

    def runner(
        args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError((args, timeout_s))

    with pytest.raises(ToolchainError) as caught:
        find_toolchain(runner=runner, which=lambda _: None)

    assert "soffice" in str(caught.value)
    assert INSTALL_GUIDE in str(caught.value)
    _assert_install_guide_order(str(caught.value))
    assert called is False


def test_find_toolchain_missing_h2orestart_and_unopkg_failure_are_actionable(
    tmp_path: Path,
) -> None:
    soffice = tmp_path / "soffice"
    unopkg = tmp_path / "unopkg"
    soffice.touch()
    unopkg.touch()

    def missing_extension_runner(
        args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        if "--version" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="LibreOffice 26.8\nsecond line\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args, 0, stdout="Identifier: another.extension\nVersion: 1.0\n", stderr=""
        )

    with pytest.raises(ToolchainError) as missing:
        find_toolchain(soffice=soffice, runner=missing_extension_runner)
    assert H2ORESTART_ID in str(missing.value)
    _assert_install_guide_order(str(missing.value))

    def locked_runner(
        args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            0 if "--version" in args else 1,
            stdout="LibreOffice 26.8\n" if "--version" in args else "",
            stderr="locked",
        )

    with pytest.raises(ToolchainError, match="LibreOffice 창을 모두 닫고 다시 실행"):
        find_toolchain(soffice=soffice, runner=locked_runner)


def test_find_toolchain_prefers_explicit_path_and_reads_version_from_same_block(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit" / "soffice"
    sibling_unopkg = explicit.with_name("unopkg")
    explicit.parent.mkdir()
    explicit.touch()
    sibling_unopkg.touch()
    calls: list[list[str]] = []

    def runner(
        args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if "--version" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="LibreOffice 26.8.1\nextra\n", stderr=""
            )
        output = (
            "Identifier: another.extension\nVersion: 99\n\n"
            f"Identifier: {H2ORESTART_ID}\nVersion: 0.7.14\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    toolchain = find_toolchain(
        soffice=explicit,
        runner=runner,
        which=lambda name: f"/wrong/{name}",
    )

    assert toolchain.soffice == explicit
    assert toolchain.unopkg == sibling_unopkg
    assert toolchain.soffice_version == "LibreOffice 26.8.1"
    assert toolchain.h2orestart_version == "0.7.14"
    assert calls == [[str(explicit), "--version"], [str(sibling_unopkg), "list"]]


def test_find_toolchain_uses_macos_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac_soffice = tmp_path / "LibreOffice.app/Contents/MacOS/soffice"
    mac_unopkg = mac_soffice.with_name("unopkg")
    mac_soffice.parent.mkdir(parents=True)
    mac_soffice.touch()
    mac_unopkg.touch()
    monkeypatch.setattr(convert, "MACOS_SOFFICE", mac_soffice)

    def runner(
        args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        stdout = (
            "LibreOffice 26.8\n"
            if "--version" in args
            else f"Identifier: {H2ORESTART_ID}\n  Version: 0.7.14\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    toolchain = find_toolchain(runner=runner, which=lambda _: None)

    assert toolchain.soffice == mac_soffice
    assert toolchain.unopkg == mac_unopkg


def test_cli_toolchain_error_returns_two_without_starting_conversion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(**kwargs: object) -> Toolchain:
        raise ToolchainError("soffice가 없습니다.")

    def forbidden_run(*args: object, **kwargs: object) -> ConvertSummary:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(convert, "find_toolchain", unavailable)
    monkeypatch.setattr(convert, "run", forbidden_run)

    assert convert.main([]) == 2
    error = capsys.readouterr().err
    assert "soffice" in error
    _assert_install_guide_order(error)


def test_output_path_contains_safe_original_extension_and_hash(tmp_path: Path) -> None:
    record = {
        "rule_no": "16538",
        "file_name": "폴더/사택 운영지침(2024년도 11월 개정).hwp",
        "sha256": "3fa1c2d4e5f6" + "0" * 52,
    }

    assert output_pdf_path(tmp_path, record) == (
        tmp_path
        / "data/raw/pdf/16538/폴더_사택 운영지침(2024년도 11월 개정).hwp.3fa1c2d4e5f6.pdf"
    )


def test_same_stem_hwp_and_hwpx_create_distinct_pdfs_in_distinct_outdirs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True)
    hwp = source_dir / "a.hwp"
    hwpx = source_dir / "a.hwpx"
    hwp.write_bytes(hwp5_bytes())
    hwpx.write_bytes(_hwpx_bytes(tmp_path / "source.hwpx"))
    records = [
        _source_record(
            tmp_path, "data/raw/alio/10/a.hwp", file_no="1", file_name="a.hwp"
        ),
        _source_record(
            tmp_path,
            "data/raw/alio/10/a.hwpx",
            file_no="2",
            file_name="a.hwpx",
            file_kind="hwpx",
        ),
    ]
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", records)
    runner = ConversionRunner(pages=2)

    summary = run(tmp_path, _toolchain(), runner=runner)

    output_paths = sorted((tmp_path / "data/raw/pdf/10").glob("*.pdf"))
    assert len(output_paths) == 2
    assert {path.name for path in output_paths} == {
        f"a.hwp.{records[0]['sha256'][:12]}.pdf",
        f"a.hwpx.{records[1]['sha256'][:12]}.pdf",
    }
    outdirs = [call[0][call[0].index("--outdir") + 1] for call in runner.calls]
    assert len(set(outdirs)) == 2
    assert summary.success_count == 2
    assert summary.failure_count == 0
    assert summary.total_page_count == 4
    manifest = _read_jsonl(tmp_path / "data/manifest/conversions.jsonl")
    assert [record["page_count"] for record in manifest] == [2, 2]
    assert all(record["status"] == "ok" for record in manifest)
    assert all(set(CONVERSION_REQUIRED_FIELDS) <= record.keys() for record in manifest)
    for args, timeout_s in runner.calls:
        assert "--infilter=Hwp2002_File" in args
        assert "pdf:writer_pdf_Export" in args
        assert timeout_s == 180


def test_signature_excludes_hwp3_ole_other_and_html_without_runner(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True)
    contents = {
        "old.hwp": HWP3_HEADER,
        "legacy.hwp": ole_other_bytes(),
        "download.hwp": b"<html>not a document</html>",
    }
    records = []
    for index, (name, content) in enumerate(contents.items(), start=1):
        path = source_dir / name
        path.write_bytes(content)
        records.append(
            _source_record(
                tmp_path,
                f"data/raw/alio/10/{name}",
                file_no=str(index),
                file_name=name,
                file_kind="hwp5",
            )
        )
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", records)
    runner = ConversionRunner()

    summary = run(tmp_path, _toolchain(), runner=runner)

    assert runner.calls == []
    assert summary.excluded_file_kinds == {"html": 1, "hwp3": 1, "ole_other": 1}
    assert summary.success_count == summary.failure_count == 0
    assert not (tmp_path / "data/manifest/conversions.jsonl").exists()


def test_missing_pdf_and_timeout_are_recorded_then_next_file_continues(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True)
    names = ["missing.hwp", "timeout.hwp", "good.hwp"]
    records = []
    for index, name in enumerate(names, start=1):
        path = source_dir / name
        path.write_bytes(hwp5_bytes())
        records.append(
            _source_record(
                tmp_path,
                f"data/raw/alio/10/{name}",
                file_no=str(index),
                file_name=name,
            )
        )
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", records)
    runner = ConversionRunner(
        missing_for=frozenset({"missing.hwp"}),
        timeout_for=frozenset({"timeout.hwp"}),
    )

    summary = run(tmp_path, _toolchain(), runner=runner)

    manifest = _read_jsonl(tmp_path / "data/manifest/conversions.jsonl")
    assert [record["status"] for record in manifest] == ["failed", "failed", "ok"]
    assert "LibreOffice 창을 모두 닫고 재시도" in str(manifest[0]["error"])
    assert "timeout" in str(manifest[1]["error"]).lower()
    assert manifest[2]["error"] is None
    assert summary.success_count == 1
    assert summary.failure_count == 2
    assert summary.total_page_count == 1


def test_success_is_idempotent_but_failed_record_is_retried(tmp_path: Path) -> None:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True)
    source = source_dir / "a.hwp"
    source.write_bytes(hwp5_bytes())
    record = _source_record(
        tmp_path, "data/raw/alio/10/a.hwp", file_no="1", file_name="a.hwp"
    )
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", [record])
    output = output_pdf_path(tmp_path, record)
    failed_record = {
        "source_path": record["local_path"],
        "source_sha256": record["sha256"],
        "source_kind": "hwp5",
        "rule_no": "10",
        "file_no": "1",
        "output_path": output.relative_to(tmp_path).as_posix(),
        "output_sha256": None,
        "page_count": 0,
        "status": "failed",
        "error": "old failure",
        "soffice_version": "old",
        "h2orestart_version": "old",
        "converted_at": "2026-01-01T00:00:00+09:00",
    }
    _write_jsonl(tmp_path / "data/manifest/conversions.jsonl", [failed_record])
    runner = ConversionRunner()

    first = run(tmp_path, _toolchain(), runner=runner)
    second = run(tmp_path, _toolchain(), runner=runner)

    assert len(runner.calls) == 1
    assert first.success_count == 1
    assert second.success_count == 0
    assert second.skipped_count == 1
    manifest = _read_jsonl(tmp_path / "data/manifest/conversions.jsonl")
    assert [record["status"] for record in manifest] == ["failed", "ok"]
    assert manifest[-1]["output_sha256"] == sha256_file(output)


def test_only_eligible_uses_version_match_sha_and_latest_alio_record(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True)
    stale = source_dir / "stale.hwp"
    current = source_dir / "current.hwp"
    excluded = source_dir / "excluded.hwp"
    for path in (stale, current, excluded):
        path.write_bytes(hwp5_bytes())
    stale_record = _source_record(
        tmp_path, "data/raw/alio/10/stale.hwp", file_no="1", file_name="stale.hwp"
    )
    current_record = _source_record(
        tmp_path,
        "data/raw/alio/10/current.hwp",
        file_no="1",
        file_name="current.hwp",
    )
    excluded_record = _source_record(
        tmp_path,
        "data/raw/alio/10/excluded.hwp",
        file_no="2",
        file_name="excluded.hwp",
    )
    _write_jsonl(
        tmp_path / "data/manifest/alio.jsonl",
        [stale_record, current_record, excluded_record],
    )
    _write_jsonl(
        tmp_path / "data/manifest/version_matches.jsonl",
        [
            {
                "alio_local_path": current_record["local_path"],
                "alio_sha256": current_record["sha256"],
                "parse_eval_eligible": True,
            },
            {
                "alio_local_path": excluded_record["local_path"],
                "alio_sha256": excluded_record["sha256"],
                "parse_eval_eligible": False,
            },
        ],
    )
    runner = ConversionRunner()

    summary = run(tmp_path, _toolchain(), runner=runner, only_eligible=True)

    assert [Path(call[0][-1]).name for call in runner.calls] == ["current.hwp"]
    assert summary.success_count == 1


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _single_hwp_source(tmp_path: Path, name: str) -> dict[str, object]:
    source_dir = tmp_path / "data/raw/alio/10"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / name).write_bytes(hwp5_bytes())
    record = _source_record(
        tmp_path, f"data/raw/alio/10/{name}", file_no="1", file_name=name
    )
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", [record])
    return record


@pytest.mark.parametrize(
    "output_name",
    [
        # APFS는 NFC/NFD를 같은 이름으로 찾으므로 이 경우는 Linux에서만 차이가 난다.
        lambda source: unicodedata.normalize("NFD", source.stem) + ".pdf",
        lambda source: "converted.pdf",
    ],
    ids=["nfd-stem", "different-name"],
)
def test_single_pdf_with_unexpected_name_is_accepted(
    tmp_path: Path, output_name: Callable[[Path], str]
) -> None:
    """soffice가 원본 stem과 다른 이름으로 써도 outdir의 유일한 PDF를 쓴다."""
    record = _single_hwp_source(tmp_path, "사택 운영지침(15.5.12).hwp")
    runner = ConversionRunner(output_names=lambda source: [output_name(source)])

    summary = run(tmp_path, _toolchain(), runner=runner)

    assert summary.success_count == 1
    assert output_pdf_path(tmp_path, record).is_file()


def test_multiple_pdfs_in_outdir_fail(tmp_path: Path) -> None:
    _single_hwp_source(tmp_path, "a.hwp")
    runner = ConversionRunner(output_names=lambda source: ["a.pdf", "b.pdf"])

    summary = run(tmp_path, _toolchain(), runner=runner)

    assert summary.failure_count == 1
    manifest = _read_jsonl(tmp_path / "data/manifest/conversions.jsonl")
    assert "PDF 여러 개" in str(manifest[0]["error"])


def test_same_sha_direct_file_and_zip_member_are_idempotent(tmp_path: Path) -> None:
    """같은 내용이 직접 첨부와 zip 멤버로 있어도 두 번째 실행은 둘 다 건너뛴다."""
    content = hwp5_bytes()
    direct = tmp_path / "data/raw/alio/10/1__규정.hwp"
    member = tmp_path / "data/raw/alio/10/2__zip/규정(2020년).hwp"
    for path in (direct, member):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    records = [
        _source_record(
            tmp_path, "data/raw/alio/10/1__규정.hwp", file_no="1", file_name="규정.hwp"
        ),
        _source_record(
            tmp_path,
            "data/raw/alio/10/2__zip/규정(2020년).hwp",
            file_no="2",
            file_name="규정(2020년).hwp",
            source="alio_zip_member",
            zip_member_path="규정(2020년).hwp",
        ),
    ]
    assert records[0]["sha256"] == records[1]["sha256"]
    _write_jsonl(tmp_path / "data/manifest/alio.jsonl", records)
    runner = ConversionRunner()
    manifest_path = tmp_path / "data/manifest/conversions.jsonl"

    first = run(tmp_path, _toolchain(), runner=runner)
    lines_after_first = len(_read_jsonl(manifest_path))
    second = run(tmp_path, _toolchain(), runner=runner)

    assert first.success_count == 2
    assert second.success_count == 0
    assert second.skipped_count == 2
    assert len(runner.calls) == 2
    assert len(_read_jsonl(manifest_path)) == lines_after_first == 2


def test_conversion_passes_repo_fontconfig_file_to_soffice(tmp_path: Path) -> None:
    """FONTCONFIG_FILE 없이 실행하면 한글 글꼴이 대체되지 않아 한글이 빈칸이 된다."""
    _single_hwp_source(tmp_path, "a.hwp")
    runner = ConversionRunner()

    run(tmp_path, _toolchain(), runner=runner)

    assert len(runner.envs) == 1
    env = runner.envs[0]
    assert env is not None
    assert env["FONTCONFIG_FILE"] == str(
        (tmp_path / "infra/fontconfig/fonts.conf").resolve()
    )
    assert "PATH" in env


def test_missing_fontconfig_file_stops_before_any_conversion(tmp_path: Path) -> None:
    _single_hwp_source(tmp_path, "a.hwp")
    (tmp_path / "infra/fontconfig/fonts.conf").unlink()
    runner = ConversionRunner()

    with pytest.raises(convert.ConversionError, match="fonts.conf"):
        run(tmp_path, _toolchain(), runner=runner)

    assert runner.calls == []


def test_repo_fonts_conf_maps_every_substitution_to_hcr_fonts() -> None:
    """alias와 match 규칙이 모두 HCR Batang/HCR Dotum만 대체 글꼴로 쓴다."""
    import xml.etree.ElementTree as ET

    root = ET.parse(REPO_FONTS_CONF).getroot()
    aliases = root.findall("alias")
    matches = root.findall("match")
    assert aliases
    assert matches
    for alias in aliases:
        preferred = [family.text for family in alias.findall("prefer/family")]
        assert preferred in (["HCR Batang"], ["HCR Dotum"])
    for match in matches:
        edited = [value.text for value in match.findall("edit/string")]
        assert edited in (["HCR Batang"], ["HCR Dotum"])
    assert "~/Library/Fonts" in [d.text for d in root.findall("dir")]


def test_repo_fonts_conf_pins_last_resort_fallback_to_hcr_dotum() -> None:
    """목록에 없는 글꼴이 히라기노 같은 시스템 임의 글꼴로 떨어지지 않게 한다."""
    import xml.etree.ElementTree as ET

    root = ET.parse(REPO_FONTS_CONF).getroot()
    fallbacks = [
        match
        for match in root.findall("match")
        if match.get("target") == "pattern"
        and (edit := match.find("edit")) is not None
        and edit.get("name") == "family"
        and edit.get("mode") == "append_last"
        and edit.get("binding") == "weak"
        and edit.findtext("string") == "HCR Dotum"
    ]
    assert len(fallbacks) == 1
    assert list(root)[-1] is fallbacks[0]


def test_repo_fonts_conf_maps_yoon_myeongjo_140_to_hcr_batang() -> None:
    import xml.etree.ElementTree as ET

    root = ET.parse(REPO_FONTS_CONF).getroot()
    mapped = {
        alias.findtext("family"): alias.findtext("prefer/family")
        for alias in root.findall("alias")
    }
    assert mapped["-윤명조140"] == "HCR Batang"


def test_pdf_font_stats_strip_subset_prefix_and_count_distinct_hangul(
    tmp_path: Path,
) -> None:
    from pypdf.generic import DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    fonts = DictionaryObject()
    for key, base_font in (
        ("/F1", "/ABCDEF+HCRBatang"),
        ("/F2", "/GHIJKL+HCRBatang"),
        ("/F3", "/LiberationSerif"),
    ):
        fonts[NameObject(key)] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/TrueType"),
                NameObject("/BaseFont"): NameObject(base_font),
            }
        )
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    path = tmp_path / "fonts.pdf"
    with path.open("wb") as output:
        writer.write(output)

    assert convert.embedded_font_names(path) == ["HCRBatang", "LiberationSerif"]
    assert convert.hangul_distinct_count("가가나 abc 다라 ㄱ 한") == 5


def test_ok_record_includes_embedded_fonts_and_hangul_distinct(tmp_path: Path) -> None:
    _single_hwp_source(tmp_path, "a.hwp")

    run(tmp_path, _toolchain(), runner=ConversionRunner())

    record = _read_jsonl(tmp_path / "data/manifest/conversions.jsonl")[0]
    assert record["status"] == "ok"
    assert record["embedded_fonts"] == []
    assert record["hangul_distinct"] == 0


def test_install_guide_mentions_optional_hcr_fonts_and_license() -> None:
    assert "함초롬 글꼴" in INSTALL_GUIDE
    assert "~/Library/Fonts" in INSTALL_GUIDE
    assert "상업적 재배포·수정 금지" in INSTALL_GUIDE
