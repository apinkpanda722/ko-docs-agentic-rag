"""Architecture boundary tests."""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_PACKAGE = PROJECT_ROOT / "src" / "kodocs" / "retrieval"
FORBIDDEN_IMPORT_PREFIXES = ("langchain", "langgraph")


def _imported_top_level_modules(path: Path) -> set[str]:
    """Parse a file with AST and collect imported top-level module names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", maxsplit=1)[0])

    return modules


def test_retrieval_package_exists() -> None:
    """Ensure the guarded package exists so the test cannot silently do nothing."""
    assert RETRIEVAL_PACKAGE.is_dir(), (
        f"retrieval package not found: {RETRIEVAL_PACKAGE}"
    )


def test_retrieval_does_not_import_langchain_or_langgraph() -> None:
    """Reject forbidden imports from every Python file in the retrieval package."""
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {module}"
        for path in sorted(RETRIEVAL_PACKAGE.rglob("*.py"))
        for module in sorted(_imported_top_level_modules(path))
        if module.startswith(FORBIDDEN_IMPORT_PREFIXES)
    ]

    assert not violations, "Forbidden retrieval imports found:\n" + "\n".join(
        violations
    )
