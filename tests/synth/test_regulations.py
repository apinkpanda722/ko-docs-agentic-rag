"""Tests for the committable, title-only regulation reference index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodocs.synth.regulations import build_index, main

FIXTURES = Path(__file__).parent / "fixtures"


def _all_string_values(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _all_string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_string_values(nested)


def test_build_index_uses_only_current_non_deleted_article_titles() -> None:
    result = build_index(FIXTURES / "law", banned_names=("NIA",))

    assert result == {
        "excluded": [{"reason": "banned_name_in_title", "rule_id": "200"}],
        "generated_from": "data/interim/law (현행)",
        "rule_count": 1,
        "rules": {
            "출장규칙": {
                "articles": {
                    "제10조의2": "정산 기한",
                    "제8조": "여비 및 여행일수의 계산",
                },
                "rule_id": "100",
                "serial": "current",
            }
        },
    }
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    assert "이전 제목" not in serialized
    assert "제9조" not in serialized
    assert "제8조(여비 및 여행일수의 계산)" not in serialized


def test_build_index_is_deterministic() -> None:
    first = build_index(FIXTURES / "law", banned_names=("NIA",))
    second = build_index(FIXTURES / "law", banned_names=("NIA",))

    assert first == second
    assert json.dumps(
        first, ensure_ascii=False, sort_keys=True, indent=2
    ) == json.dumps(second, ensure_ascii=False, sort_keys=True, indent=2)


def test_build_index_rejects_zero_current_rules(tmp_path: Path) -> None:
    law_dir = tmp_path / "law"
    rule_dir = law_dir / "100"
    rule_dir.mkdir(parents=True)
    (rule_dir / "old.json").write_text(
        json.dumps(
            {
                "articles": ["제1조(목적)"],
                "is_current": False,
                "name_without_agency": "출장규칙",
                "rule_id": "100",
                "serial": "old",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="현행 규정이 0개"):
        build_index(law_dir, banned_names=())


def test_build_index_rejects_current_rule_with_zero_live_articles(
    tmp_path: Path,
) -> None:
    law_dir = tmp_path / "law"
    rule_dir = law_dir / "100"
    rule_dir.mkdir(parents=True)
    (rule_dir / "current.json").write_text(
        json.dumps(
            {
                "articles": ["제1조 삭제"],
                "is_current": True,
                "name_without_agency": "출장규칙",
                "rule_id": "100",
                "serial": "current",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="유효한 조가 0개"):
        build_index(law_dir, banned_names=())


def test_main_returns_two_with_guidance_when_interim_law_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path)]) == 2
    assert "data/interim/law" in capsys.readouterr().err


def test_committed_index_exists_and_contains_no_article_body() -> None:
    path = Path("data/manifest/regulation_refs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["rule_count"] >= 90
    assert "제8조" in payload["rules"]["출장규칙"]["articles"]
    assert max(map(len, _all_string_values(payload))) <= 80
