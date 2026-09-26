"""Tests for local HWP/HWPX to PDF conversion orchestration."""

import json
import subprocess
from collections.abc import Sequence
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
    ) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.missing_for = missing_for
        self.timeout_for = timeout_for
        self.pages = pages

    def __call__(
        self, args: Sequence[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        call = [str(arg) for arg in args]
        self.calls.append((call, timeout_s))
        source = Path(call[-1])
        if source.name in self.timeout_for:
            raise subprocess.TimeoutExpired(call, timeout_s)
        if source.name not in self.missing_for:
            outdir = Path(call[call.index("--outdir") + 1])
            (outdir / f"{source.stem}.pdf").write_bytes(_pdf_bytes(self.pages))
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
