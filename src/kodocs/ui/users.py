"""Load only public demo labels and server-owned user attributes."""

import json
from dataclasses import dataclass
from pathlib import Path

from kodocs.tools import UserContext


@dataclass(frozen=True)
class DemoUser:
    """A selectable demo identity without authentication material."""

    label: str
    team: str
    grade: int

    def to_user_context(self) -> UserContext:
        return UserContext(team=self.team, grade=self.grade)


def load_demo_users(
    path: Path = Path("data/manifest/mcp_token_hashes.json"),
) -> tuple[DemoUser, ...]:
    """Project a committed manifest through an explicit field whitelist."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("데모 사용자 manifest를 읽을 수 없습니다.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), list):
        raise TypeError("데모 사용자 manifest의 users 목록이 필요합니다.")
    users = []
    seen = set()
    labels = set()
    for raw in payload["users"]:
        if not isinstance(raw, dict):
            raise TypeError("데모 사용자 항목은 객체여야 합니다.")
        fields = {}
        for name in ("label", "team", "grade"):
            if name not in raw:
                raise ValueError(f"데모 사용자 필수 필드 누락: {name}")
            fields[name] = raw[name]
        for name in ("label", "team"):
            if not isinstance(fields[name], str) or not fields[name].strip():
                raise ValueError(
                    f"데모 사용자 {name}은 비어 있지 않은 문자열이어야 합니다."
                )
        grade = fields["grade"]
        if isinstance(grade, bool) or not isinstance(grade, int) or grade < 1:
            raise ValueError("데모 사용자 grade는 양의 정수여야 합니다.")
        combination = (fields["team"], grade)
        if combination in seen:
            continue
        if fields["label"] in labels:
            raise ValueError("데모 사용자 label이 중복되었습니다.")
        users.append(DemoUser(**fields))
        seen.add(combination)
        labels.add(fields["label"])
    if not users:
        raise ValueError("데모 사용자 users 목록이 비어 있습니다.")
    return tuple(users)
