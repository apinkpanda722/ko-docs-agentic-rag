"""Demo token generation and constant-time verification tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from kodocs.mcp_server import tokens


def _write_queries(root: Path) -> None:
    directory = root / "data" / "golden" / "queries"
    directory.mkdir(parents=True)
    (directory / "gold.jsonl").write_text(
        '{"asker":{"team":"platform","grade":3}}\n'
        '{"asker":{"team":"security","grade":1}}\n',
        encoding="utf-8",
    )
    (directory / "silver.jsonl").write_text(
        '{"asker":{"team":"platform","grade":3}}\n'
        '{"asker":{"team":"finance","grade":2}}\n',
        encoding="utf-8",
    )


def test_generate_tokens_writes_plaintext_only_to_ignored_interim_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_queries(tmp_path)
    generated = iter(("plain-token-a", "plain-token-b", "plain-token-c"))
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _size: next(generated))

    assert tokens.main(["--root", str(tmp_path)]) == 0

    plaintext_path = tmp_path / "data/interim/mcp/tokens.json"
    hash_path = tmp_path / "data/manifest/mcp_token_hashes.json"
    plaintext = json.loads(plaintext_path.read_text(encoding="utf-8"))
    hashes = json.loads(hash_path.read_text(encoding="utf-8"))
    output = capsys.readouterr()

    assert [item["label"] for item in plaintext["users"]] == [
        "demo-finance-g2",
        "demo-platform-g3",
        "demo-security-g1",
    ]
    assert {item["token"] for item in plaintext["users"]} == {
        "plain-token-a",
        "plain-token-b",
        "plain-token-c",
    }
    assert set(hashes) == {"users"}
    assert all(
        set(item) == {"label", "team", "grade", "token_sha256"}
        for item in hashes["users"]
    )
    for token_value in ("plain-token-a", "plain-token-b", "plain-token-c"):
        assert token_value not in hash_path.read_text(encoding="utf-8")
        assert token_value not in output.out
        assert token_value not in output.err
    assert "3" in output.out
    assert "demo-platform-g3" in output.out
    assert plaintext_path.stat().st_mode & 0o077 == 0


def test_repository_gitignore_covers_plaintext_token_path(project_root: Path) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "data/interim/mcp/tokens.json"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "data/interim/mcp/tokens.json"


@pytest.mark.parametrize("existing", ["plain", "hash"])
def test_generation_refuses_if_either_output_already_exists(
    tmp_path: Path,
    existing: str,
) -> None:
    _write_queries(tmp_path)
    plain = tmp_path / "data/interim/mcp/tokens.json"
    hashed = tmp_path / "data/manifest/mcp_token_hashes.json"
    target = plain if existing == "plain" else hashed
    target.parent.mkdir(parents=True)
    target.write_text("original", encoding="utf-8")

    assert tokens.main(["--root", str(tmp_path)]) == 1
    assert target.read_text(encoding="utf-8") == "original"
    assert not (hashed if existing == "plain" else plain).exists()


def test_rotate_replaces_both_outputs_without_printing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_queries(tmp_path)
    plain = tmp_path / "data/interim/mcp/tokens.json"
    hashed = tmp_path / "data/manifest/mcp_token_hashes.json"
    plain.parent.mkdir(parents=True)
    hashed.parent.mkdir(parents=True)
    plain.write_text("old", encoding="utf-8")
    hashed.write_text("old", encoding="utf-8")
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _size: "rotated-secret")

    assert tokens.main(["--root", str(tmp_path), "--rotate"]) == 0

    assert "rotated-secret" in plain.read_text(encoding="utf-8")
    assert "rotated-secret" not in hashed.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "rotated-secret" not in captured.out
    assert "rotated-secret" not in captured.err


def test_hash_store_uses_compare_digest_and_returns_server_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_value = "constant-time-token"
    path = tmp_path / "hashes.json"
    path.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "label": "demo-platform-g3",
                        "team": "platform",
                        "grade": 3,
                        "token_sha256": hashlib.sha256(
                            token_value.encode()
                        ).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []
    real_compare = tokens.hmac.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(tokens.hmac, "compare_digest", recording_compare)
    store = tokens.TokenHashStore.load(path)

    assert store.authenticate(token_value) == tokens.UserContext("platform", 3)
    assert store.authenticate("wrong-token") is None
    assert len(calls) == 2
