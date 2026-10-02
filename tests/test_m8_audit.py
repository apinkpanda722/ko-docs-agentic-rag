"""Public audit uses fake data, read-only subprocesses and value-free reports."""

import json
import subprocess
from pathlib import Path

import pytest

from kodocs.eval import m8_audit as audit
from kodocs.eval import m8_audit_gh as github


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    def commit():
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fake-local@gmail.com",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )

    (tmp_path / "sample.txt").write_text("ordinary text")
    commit()
    return tmp_path, commit


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("anthropic", "sk-ant-" + "X" * 40),
        ("openai", "sk-" + "Z" * 40),
        ("google", "AIza" + "G" * 35),
        ("upstage", "up_" + "U" * 40),
        ("law_oc", "?OC=fakeCredential&target=law"),
        ("assignment", 'api_key="fakeCredential123456"'),
        ("mcp_plaintext", '"token": "' + "M" * 43 + '"'),
    ],
)
def test_patterns(name, value):
    assert audit.scan_text(value)["patterns"][name] == 1
    assert (
        name
        not in audit.scan_text("a" * 64 + " 12345678-1234-1234-1234-123456789abc")[
            "patterns"
        ]
    )
    assert not audit.scan_text('token_sha256="' + "a" * 64 + '"')["patterns"]


def test_non_disclosure_and_history(repo, capsys, monkeypatch):
    root, commit = repo
    secret = "sk-ant-" + "Q" * 40
    phrase = "가상의규정조문만사용하며실제기관의규정원문은아닌테스트용문장입니다"
    (root / "deleted.txt").write_text(secret + "\n" + phrase)
    (root / "binary.bin").write_bytes(b"\xff\xfe")
    commit()
    (root / "deleted.txt").unlink()
    commit()
    law = root / "data/interim/law/fake.json"
    law.parent.mkdir(parents=True)
    law.write_text(json.dumps({"articles": [phrase]}))
    original = audit.subprocess.run
    calls = []

    def checked(args, **kwargs):
        assert args[0] == "git" and args[1] in audit.GIT_COMMANDS
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", checked)
    result = audit.run_audit(root)
    audit.write_outputs(root, result)
    outputs = (root / audit.MANIFEST).read_text() + (root / audit.REPORT).read_text()
    assert (
        secret not in outputs and phrase not in outputs and "fake-local" not in outputs
    )
    assert result["history"]["undecodable"] == 1
    assert result["history"]["scanned_blobs"] > 0
    assert any(
        r["max_overlap_chars"] >= len(phrase) for r in result["history"]["findings"]
    )
    assert result["email_domains"]["author"]["gmail.com"]["count"] == 3
    assert calls
    assert not capsys.readouterr().out
    original_report = (root / audit.REPORT).read_text() + "\nuser note\n"
    (root / audit.REPORT).write_text(original_report)
    audit.write_outputs(root, result)
    assert (root / audit.REPORT).read_text().endswith("\nuser note\n")

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(audit.subprocess, "run", fail)
    with pytest.raises(audit.AuditError) as exc:
        audit.run_audit(root)
    assert secret not in str(exc.value)
    assert audit.main(root=root) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_empty_history_is_error(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(audit.AuditError):
        audit.run_audit(tmp_path)


def test_shingles_normalize_and_skip(tmp_path):
    reference = audit.shingle_hashes("가" * 35)
    assert audit.scan_text("가 " * 35, reference)["max_overlap_chars"] == 35
    assert audit.load_sources(tmp_path)[1]["status"] == "skipped: 로컬 원문 없음"


def test_paths_and_inventory(repo):
    root, commit = repo
    for p in [".env.example", "data/raw/a.pdf", "data/golden/tables/README.md"]:
        target = root / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    (root / "src").mkdir()
    (root / "src/example.py").write_text('call("rev-parse")\n')
    head = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%H"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest = root / "data/manifest/a.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {"code_commit": head, "nested": {"commit": head}, "sha256": "a" * 64}
        )
    )
    commit()
    result = audit.run_audit(root)
    assert result["commit_inventory"]["pairs"] == 1
    assert result["commit_inventory"]["existing"] == 1
    assert result["commit_inventory"]["rev_parse"] == [
        {"path": "src/example.py", "line": 1}
    ]
    assert result["paths"]["current"]["raw"]["count"] == 1
    assert "env" not in result["paths"]["current"]
    assert "golden_tables" not in result["paths"]["current"]


def test_github_get_only_and_partial_failure(repo, monkeypatch):
    root, _ = repo
    secret = "sk-" + "T" * 40
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        if args[0] == "git":
            return subprocess.CompletedProcess(
                args, 0, b"git@github.com:fake/repository.git\n", b""
            )
        assert args[:2] == ["gh", "api"]
        assert not set(args) & {"POST", "PATCH", "PUT", "DELETE", "-f", "-F", "--input"}
        assert args[args.index("--method") + 1] == "GET"
        endpoint = args[2]
        if "/reviews?" in endpoint:
            return subprocess.CompletedProcess(args, 1, b"", secret.encode())
        if "page=2" in endpoint:
            value = []
        elif "/issues?" in endpoint:
            value = [
                {"number": 1, "body": secret},
                {"number": 2, "pull_request": {}, "body": secret},
            ]
        elif "/pulls?" in endpoint:
            value = [{"number": 2, "body": secret}]
        else:
            value = [
                {
                    "id": 55,
                    "body": secret,
                    "issue_url": "https://api.github.com/repos/fake/repository/issues/1",
                    "pull_request_url": "https://api.github.com/repos/fake/repository/pulls/2",
                }
            ]
        return subprocess.CompletedProcess(args, 0, json.dumps(value).encode(), b"")

    monkeypatch.setattr(audit.subprocess, "run", fake)
    result = github.run_audit(root)
    assert result["counts"]["prs"] == 1
    assert result["item_failures"] == 1
    assert secret not in json.dumps(result)
    assert calls


def test_make_targets():
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text()
    agents = (root / "AGENTS.md").read_text()
    for name in ("m8-audit", "m8-audit-gh"):
        assert name in makefile.splitlines()[0].split()
        assert (
            f"{name}:\n\tuv run python -m kodocs.eval.{name.replace('-', '_')}"
            in makefile
        )
        assert f"make {name}" in agents


def test_large_binary_and_sensitive_paths(repo, monkeypatch):
    root, commit = repo
    (root / ".env.private").write_text("must never read this working file")
    (root / "oversized.txt").write_bytes(b"x" * (audit.MAX_BLOB + 1))
    commit()
    original = audit.read

    def guarded(path):
        assert path.name != ".env.private"
        return original(path)

    monkeypatch.setattr(audit, "read", guarded)
    result = audit.run_audit(root)
    assert result["worktree"]["oversized"] == result["history"]["oversized"] == 1
    assert result["worktree"]["excluded_sensitive_worktree"] == 1
    assert result["paths"]["current"]["env"]["count"] == 1
    assert result["paths"]["history"]["env"]["count"] == 1
    assert result["hygiene"]["env_in_history"]


def test_assignment_avoids_hash_uuid_and_oc_empty():
    for value in ("a" * 64, "12345678-1234-1234-1234-123456789abc"):
        assert not audit.scan_text(f'api_key="{value}"')["patterns"]
    assert not audit.scan_text("?OC=&target=law")["patterns"]
    counts = audit.scan_text("010-1234-5678 담당자명 /Users/fake/project")["patterns"]
    assert counts == {"phone": 1, "personal_field": 1, "absolute_path": 1}


def test_original_read_error_and_safe_paths(repo, monkeypatch):
    root, _ = repo
    secret = "sk-" + "K" * 40
    path = "data/" + secret + "/fake-local@gmail.com.json"
    safe = audit.safe_path(path)
    assert secret not in safe and "fake-local" not in safe
    (root / "sample.txt").unlink()
    with pytest.raises(audit.AuditError) as exc:
        audit.run_audit(root)
    assert "sample.txt" not in str(exc.value)
    with pytest.raises(audit.AuditError):
        audit.git(root, "commit")
    with pytest.raises(audit.AuditError):
        audit.git(root, "remote", "set-url", "origin", secret)


def test_same_denylist_and_manifest_inventory(repo):
    root, commit = repo
    directory = root / "data/seed"
    directory.mkdir(parents=True)
    (directory / "banned_names.yaml").write_text("names:\n  - FAKEAGENCY\n")
    synthetic = root / "data/synthetic/fake.json"
    synthetic.parent.mkdir(parents=True)
    synthetic.write_text('{"body": "FAKEAGENCY FAKEAGENCYX"}')
    commit()
    result = audit.run_audit(root)
    matches = [
        x
        for x in result["worktree"]["findings"]
        if x["path"] == "data/synthetic/fake.json"
    ]
    assert matches[0]["patterns"]["real_institution"] == 1
    assert "FAKEAGENCY" not in json.dumps(result)


def test_github_pagination_invalid_item_continues(repo, monkeypatch, capsys):
    root, _ = repo
    secret = "sk-ant-" + "B" * 40

    def fake(args, **kwargs):
        if args[0] == "git":
            return subprocess.CompletedProcess(
                args, 0, b"https://github.com/fake/repository.git", b""
            )
        if "/issues?" in args[2] and "&page=1" in args[2]:
            items = [
                {"number": 1, "body": secret},
                {"number": 2, "body": {"bad": secret}},
            ]
        elif "/issues?" in args[2] and "&page=2" in args[2]:
            items = [{"number": 3, "body": secret}]
        else:
            items = []
        return subprocess.CompletedProcess(args, 0, json.dumps(items).encode(), b"")

    monkeypatch.setattr(audit.subprocess, "run", fake)
    result = github.run_audit(root)
    assert result["counts"]["issues"] == 2
    assert result["item_failures"] == 1
    assert github.main(root=root) == 0
    outputs = (root / github.MANIFEST).read_text() + capsys.readouterr().out
    assert secret not in outputs

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(audit.subprocess, "run", fail)
    assert github.main(root=root) == 1
    assert secret not in capsys.readouterr().err


def test_inventory_uses_actual_files_without_fixed_counts(repo):
    root, _ = repo
    (root / "src").mkdir()
    (root / "src/new.py").write_text('call("rev-parse")\ncall("rev-parse")\n')
    result = audit.commit_inventory(root, [])
    expected = [
        {"path": str(p.relative_to(root)), "line": i}
        for p in sorted((root / "src").rglob("*"))
        if p.is_file()
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if "rev-parse" in line
    ]
    assert result["rev_parse"] == expected
    assert result["rev_parse_calls"] == len(expected)


def test_hangul_shingles_threshold_and_contiguous_length():
    for text, expected in [
        ("가나다라마바사아자차카타파하거너더러머버" + "x" * 10, 1),
        ("가" * 19 + "x" * 11, 0),
        ("=" * 40, 0),
        ("x" * 40, 0),
        ("print(value); return value; code()", 0),
    ]:
        result = audit.scan_text(text, audit.shingle_hashes(text))
        assert result["hangul_overlap_shingles"] == expected
        assert result["overlap_shingles"] == len(audit.normalize(text)) - 29
    text = "가 " * 35
    result = audit.scan_text(text, audit.shingle_hashes(text))
    assert result["hangul_max_overlap_chars"] == result["max_overlap_chars"] == 35


def test_large_report_is_tables_bounded_and_grouped(repo):
    root, _ = repo
    result = audit.run_audit(root)
    findings = [
        {
            "path": f"file-{i}",
            "patterns": {"phone": i + 1},
            "overlap_shingles": i,
            "max_overlap_chars": i + 30,
            "hangul_overlap_shingles": 0,
            "hangul_max_overlap_chars": 0,
        }
        for i in range(1000)
    ]
    result["worktree"]["findings"] = findings
    result["history"]["findings"] = findings + findings
    audit.write_outputs(root, result)
    doc = (root / audit.REPORT).read_text()
    assert len(doc.splitlines()) <= 300
    assert "```json" not in doc
    assert "| file-999 |" in doc
    assert "생략" in doc
    grouped = audit.group_history(findings + findings)
    assert grouped["file-999"]["blobs"] == 2
    assert grouped["file-999"]["patterns"]["phone"] == 1000


def test_report_bound_with_many_domains_paths_and_calls(repo):
    root, _ = repo
    result = audit.run_audit(root)
    findings = [
        {
            "path": f"file-{i}",
            "patterns": dict.fromkeys(audit.PATTERNS, 1),
            "overlap_shingles": i,
            "max_overlap_chars": i + 30,
            "hangul_overlap_shingles": 0,
            "hangul_max_overlap_chars": 0,
        }
        for i in range(1000)
    ]
    result["worktree"]["findings"] = result["history"]["findings"] = findings
    result["email_domains"]["author"] = {
        f"d{i}.example": {"count": 1, "category": "other"} for i in range(1000)
    }
    result["commit_inventory"]["rev_parse"] = [
        {"path": f"src/f{i}.py", "line": i} for i in range(1000)
    ]
    audit.write_outputs(root, result)
    assert len((root / audit.REPORT).read_text().splitlines()) <= 300
