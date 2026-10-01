"""Generate demo bearer tokens and load their committed SHA-256 identities."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kodocs.tools import UserContext

PLAINTEXT_TOKEN_PATH = Path("data/interim/mcp/tokens.json")
TOKEN_HASH_PATH = Path("data/manifest/mcp_token_hashes.json")
QUERY_PATHS = (
    Path("data/golden/queries/gold.jsonl"),
    Path("data/golden/queries/silver.jsonl"),
)


@dataclass(frozen=True)
class _TokenRecord:
    label: str
    user: UserContext
    token_sha256: str


@dataclass(frozen=True)
class TokenHashStore:
    """Immutable token hashes mapped to server-owned user contexts."""

    records: tuple[_TokenRecord, ...]

    @classmethod
    def load(cls, path: Path) -> TokenHashStore:
        """Load and validate the hash-only token manifest."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"users"}:
            raise ValueError("MCP token hash manifest must contain only users")
        raw_users = payload["users"]
        if not isinstance(raw_users, list) or not raw_users:
            raise ValueError("MCP token hash manifest users must be a non-empty list")
        records: list[_TokenRecord] = []
        labels: set[str] = set()
        hashes: set[str] = set()
        for raw in raw_users:
            record = _parse_hash_record(raw)
            if record.label in labels or record.token_sha256 in hashes:
                raise ValueError("MCP token hash manifest contains duplicate users")
            labels.add(record.label)
            hashes.add(record.token_sha256)
            records.append(record)
        return cls(tuple(records))

    def authenticate(self, token: str) -> UserContext | None:
        """Match a presented token without storing or comparing its plaintext."""
        if not isinstance(token, str) or not token:
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: UserContext | None = None
        for record in self.records:
            if hmac.compare_digest(candidate, record.token_sha256):
                matched = record.user
        return matched


def generate_tokens(*, root: Path, rotate: bool = False) -> tuple[str, ...]:
    """Create plaintext demo tokens and a separate hash-only manifest."""
    plaintext_path = root / PLAINTEXT_TOKEN_PATH
    hash_path = root / TOKEN_HASH_PATH
    if not rotate and (plaintext_path.exists() or hash_path.exists()):
        raise FileExistsError(
            "MCP token outputs already exist; use --rotate to replace both files"
        )

    users = _query_users(root)
    plaintext_users: list[dict[str, object]] = []
    hash_users: list[dict[str, object]] = []
    labels: list[str] = []
    for user in users:
        label = f"demo-{user.team}-g{user.grade}"
        token = secrets.token_urlsafe(32)
        plaintext_users.append(
            {
                "label": label,
                "team": user.team,
                "grade": user.grade,
                "token": token,
            }
        )
        hash_users.append(
            {
                "label": label,
                "team": user.team,
                "grade": user.grade,
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            }
        )
        labels.append(label)

    plaintext_path.parent.mkdir(parents=True, exist_ok=True)
    hash_path.parent.mkdir(parents=True, exist_ok=True)
    plaintext_path.write_text(
        _pretty_json({"users": plaintext_users}),
        encoding="utf-8",
    )
    plaintext_path.chmod(0o600)
    hash_path.write_text(
        _pretty_json({"users": hash_users}),
        encoding="utf-8",
    )
    return tuple(labels)


def _query_users(root: Path) -> tuple[UserContext, ...]:
    users: set[tuple[str, int]] = set()
    for relative in QUERY_PATHS:
        path = root / relative
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                asker = payload["asker"]
                user = UserContext(asker["team"], asker["grade"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid asker in {relative}:{line_number}") from exc
            users.add((user.team, user.grade))
    if not users:
        raise ValueError("gold and silver queries contain no asker contexts")
    return tuple(UserContext(team, grade) for team, grade in sorted(users))


def _parse_hash_record(raw: Any) -> _TokenRecord:
    if not isinstance(raw, dict) or set(raw) != {
        "label",
        "team",
        "grade",
        "token_sha256",
    }:
        raise ValueError("MCP token hash user has invalid fields")
    label = raw["label"]
    token_sha256 = raw["token_sha256"]
    if not isinstance(label, str) or not label:
        raise ValueError("MCP token label must be a non-empty string")
    if (
        not isinstance(token_sha256, str)
        or len(token_sha256) != 64
        or any(character not in "0123456789abcdef" for character in token_sha256)
    ):
        raise ValueError("MCP token hash must be lowercase SHA-256 hex")
    user = UserContext(raw["team"], raw["grade"])
    if label != f"demo-{user.team}-g{user.grade}":
        raise ValueError("MCP token label does not match its user context")
    return _TokenRecord(label, user, token_sha256)


def _pretty_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP 데모 사용자 토큰을 생성합니다.")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args(argv)
    try:
        labels = generate_tokens(root=args.root, rotate=args.rotate)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"MCP 데모 사용자 토큰 {len(labels)}개를 생성했습니다.")
    for label in labels:
        print(label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
