"""통합 테스트가 실측 인덱스를 건드리지 못하게 막는 가드 (FP-046)."""

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
# 실측 인덱스 이름(kodocs-m5-{composite|pypdf|upstage}-{fixed|structural})
REAL_INDEX_NAME_RE = re.compile(
    r"kodocs-m5-(?:" + "composite|pypdf|upstage" + r")-(?:" + "fixed|structural" + r")"
)


def _integration_test_files() -> list[Path]:
    return [
        path
        for path in sorted(TESTS_ROOT.rglob("test_*.py"))
        if path != Path(__file__).resolve()
        and "pytest.mark.integration" in path.read_text(encoding="utf-8")
    ]


def find_real_index_name_uses(text: str) -> list[str]:
    return REAL_INDEX_NAME_RE.findall(text)


def find_unisolated_build_index_calls(text: str) -> bool:
    """build_index를 부르는데 index_name_override를 안 쓰면 True."""
    return "build_index(" in text and "index_name_override=" not in text


def find_m6_permission_write_operations(text: str) -> list[str]:
    """Reject every index creation/deletion/bulk-builder marker in the read-only test."""
    forbidden = (
        "build_index(",
        "index_body(",
        ".bulk(",
        'request("PUT"',
        'request("DELETE"',
    )
    return [marker for marker in forbidden if marker in text]


def test_integration_tests_exist_so_the_guard_is_not_vacuous() -> None:
    assert _integration_test_files()


def test_integration_tests_never_use_real_m5_index_names() -> None:
    violations = [
        f"{path.relative_to(TESTS_ROOT)}: {name}"
        for path in _integration_test_files()
        for name in find_real_index_name_uses(path.read_text(encoding="utf-8"))
    ]
    assert not violations, (
        "통합 테스트가 실측 인덱스 이름을 쓰면 DELETE로 실측 인덱스를 지운다; "
        "uuid 붙은 임시 이름을 쓰라:\n" + "\n".join(violations)
    )


def test_integration_tests_calling_build_index_inject_a_temporary_name() -> None:
    violations = [
        str(path.relative_to(TESTS_ROOT))
        for path in _integration_test_files()
        if find_unisolated_build_index_calls(path.read_text(encoding="utf-8"))
    ]
    assert not violations, (
        "build_index 호출은 index_name_override=임시 이름이 필요하다: "
        + ", ".join(violations)
    )


def test_guard_detects_real_names_and_unisolated_calls() -> None:
    assert find_real_index_name_uses('"kodocs-m5-composite-structural"') == [
        "kodocs-m5-composite-structural"
    ]
    assert find_real_index_name_uses("kodocs-m5-upstage-fixed x") == [
        "kodocs-m5-upstage-fixed"
    ]
    assert find_real_index_name_uses("kodocs-m5-integration-abc") == []
    assert find_unisolated_build_index_calls("build_index(spec, client=c)")
    assert not find_unisolated_build_index_calls(
        "build_index(spec, index_name_override=name)"
    )


def test_m6_permission_integration_is_read_only() -> None:
    path = TESTS_ROOT / "test_m6_permission_integration.py"
    assert path.is_file()

    assert find_m6_permission_write_operations(path.read_text(encoding="utf-8")) == []


def test_m6_permission_write_guard_detects_all_forbidden_operations() -> None:
    source = (
        "build_index(spec)\nindex_body(dim=2)\nclient.bulk(name, docs)\n"
        'client.request("PUT", path)\nclient.request("DELETE", path)\n'
    )

    assert find_m6_permission_write_operations(source) == [
        "build_index(",
        "index_body(",
        ".bulk(",
        'request("PUT"',
        'request("DELETE"',
    ]
