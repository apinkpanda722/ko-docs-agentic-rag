"""Build a deterministic, committable index of current regulation headings."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from kodocs.parsing.articles import law_to_lines, split_text
from kodocs.synth.seed import find_banned_names, load_banned_names

INDEX_PATH = Path("data/manifest/regulation_refs.json")
DEFAULT_BANNED_NAMES = (
    "한국지능정보사회진흥원",
    "지능정보사회진흥원",
    "한국정보화진흥원",
    "정보화진흥원",
    "한국정보사회진흥원",
    "한국전산원",
    "NIA",
)


def build_index(law_dir: Path, *, banned_names: Sequence[str]) -> dict[str, object]:
    """Index current rule names, live article keys, and titles—never bodies."""
    current: list[tuple[Path, Mapping[str, object]]] = []
    for path in sorted(law_dir.glob("*/*.json")):
        with path.open(encoding="utf-8") as source:
            payload = json.load(source)
        if not isinstance(payload, Mapping):
            raise TypeError(f"법제처 JSON 최상위 값이 객체가 아님: {path}")
        if payload.get("is_current") is True:
            current.append((path, payload))
    if not current:
        raise ValueError(f"현행 규정이 0개입니다: {law_dir}")

    current_rule_ids: set[str] = set()
    excluded: list[dict[str, str]] = []
    rules: dict[str, object] = {}
    article_count = 0
    for path, law in current:
        rule_id = _required_string(law, "rule_id", path)
        if rule_id in current_rule_ids:
            raise ValueError(f"현행 버전이 둘 이상인 규정: {rule_id}")
        current_rule_ids.add(rule_id)
        name = _required_string(law, "name_without_agency", path)
        serial = _required_string(law, "serial", path)
        if find_banned_names(name, banned_names):
            excluded.append({"rule_id": rule_id, "reason": "banned_name_in_title"})
            continue

        split = split_text(law_to_lines(law))
        articles: dict[str, str | None] = {}
        for article in split.articles:
            if article.deleted:
                continue
            key = f"제{article.number}조"
            if article.branch:
                key += f"의{article.branch}"
            if key in articles:
                raise ValueError(f"조 키가 중복됨: {name} {key}")
            articles[key] = article.title
        if name in rules:
            raise ValueError(f"현행 규정명이 중복됨: {name}")
        rules[name] = {
            "rule_id": rule_id,
            "serial": serial,
            "articles": dict(sorted(articles.items())),
        }
        article_count += len(articles)

    if not rules:
        raise ValueError("금지 이름 제외 후 색인할 현행 규정이 0개입니다")
    if article_count == 0:
        raise ValueError("현행 규정의 유효한 조가 0개입니다")
    return {
        "generated_from": "data/interim/law (현행)",
        "rule_count": len(rules),
        "rules": dict(sorted(rules.items())),
        "excluded": sorted(excluded, key=lambda item: item["rule_id"]),
    }


def load_index(root: Path) -> dict[str, object]:
    """Load the committed regulation reference index."""
    path = root / INDEX_PATH
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise TypeError(f"regulation index must be a JSON object: {path}")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Build ``data/manifest/regulation_refs.json`` from local interim data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    law_dir = root / "data/interim/law"
    if not law_dir.is_dir():
        print(
            "data/interim/law가 없습니다. 먼저 make collect-law를 사용해 로컬 원문을 "
            "수집하세요.",
            file=sys.stderr,
        )
        return 2

    banned_path = root / "data/seed/banned_names.yaml"
    try:
        banned_names = (
            load_banned_names(root) if banned_path.is_file() else DEFAULT_BANNED_NAMES
        )
        payload = build_index(law_dir, banned_names=banned_names)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"규정 색인을 만들 수 없습니다: {exc}", file=sys.stderr)
        return 2

    output = root / INDEX_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(f"규정 색인 {payload['rule_count']}건: {output}")
    return 0


def _required_string(payload: Mapping[str, object], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"필수 문자열 필드 누락: {path} ({key})")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
