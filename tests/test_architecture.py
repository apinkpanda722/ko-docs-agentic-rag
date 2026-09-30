"""Architecture boundary tests."""

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
RETRIEVAL_PACKAGE = SOURCE_ROOT / "kodocs" / "retrieval"
TOOLS_PACKAGE = SOURCE_ROOT / "kodocs" / "tools"
AGENT_PACKAGE = SOURCE_ROOT / "kodocs" / "agent"
MCP_SERVER_PACKAGE = SOURCE_ROOT / "kodocs" / "mcp_server"
CHUNKING_PACKAGE = SOURCE_ROOT / "kodocs" / "chunking"
EVAL_PACKAGE = SOURCE_ROOT / "kodocs" / "eval"

RETRIEVAL_FORBIDDEN_IMPORTS = (
    "langchain*",
    "langgraph*",
    "kodocs.agent",
    "kodocs.mcp_server",
    "kodocs.ui",
    "kodocs.tools",
)
TOOLS_FORBIDDEN_IMPORTS = (
    "kodocs.agent",
    "kodocs.mcp_server",
    "kodocs.ui",
    "kodocs.llm",
    "langchain*",
    "langgraph*",
    "mcp",
    "openai",
    "anthropic",
)
RETRIEVAL_CONSUMER_FORBIDDEN_IMPORTS = ("kodocs.retrieval",)
M5_LLM_FORBIDDEN_IMPORTS = ("anthropic", "openai", "google", "kodocs.llm")


def _current_package(path: Path, package_root: Path) -> tuple[str, ...]:
    """Return the dotted package containing ``path`` relative to an import root."""
    return path.relative_to(package_root).parent.parts


def _resolve_relative_module(
    module: str,
    level: int,
    current_package: tuple[str, ...],
) -> str:
    """Resolve an AST relative-import name, tolerating over-deep test fixtures."""
    if level == 0:
        return module

    retained_parts = max(len(current_package) - level + 1, 0)
    parts = (*current_package[:retained_parts], *module.split("."))
    return ".".join(part for part in parts if part)


def _import_references(path: Path, package_root: Path) -> list[tuple[int, str]]:
    """Collect static and literal dynamic imports as absolute module references."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current_package = _current_package(path, package_root)
    references: set[tuple[int, str]] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.update((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative_module(
                node.module or "",
                node.level,
                current_package,
            )
            if base:
                references.add((node.lineno, base))
            references.update(
                (node.lineno, ".".join(part for part in (base, alias.name) if part))
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            is_import_module = (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                and function.attr == "import_module"
            )
            is_builtin_import = (
                isinstance(function, ast.Name) and function.id == "__import__"
            )
            literal = node.args[0]
            if (
                (is_import_module or is_builtin_import)
                and isinstance(literal, ast.Constant)
                and isinstance(literal.value, str)
            ):
                module = literal.value
                level = len(module) - len(module.lstrip("."))
                references.add(
                    (
                        node.lineno,
                        _resolve_relative_module(
                            module[level:],
                            level,
                            current_package,
                        ),
                    )
                )

    return sorted(references)


def _matches_forbidden_pattern(module: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return module.startswith(pattern.removesuffix("*"))
    return module == pattern or module.startswith(f"{pattern}.")


def _find_imports_matching(
    path: Path,
    package_root: Path,
    forbidden_patterns: tuple[str, ...],
) -> list[str]:
    return [
        f"line {line}: {module}"
        for line, module in _import_references(path, package_root)
        if any(
            _matches_forbidden_pattern(module, pattern)
            for pattern in forbidden_patterns
        )
    ]


def find_forbidden_imports(
    path: Path,
    package_root: Path,
    forbidden_patterns: tuple[str, ...] = RETRIEVAL_FORBIDDEN_IMPORTS,
) -> list[str]:
    """Find imports forbidden by one package layer boundary."""
    return _find_imports_matching(path, package_root, forbidden_patterns)


def _write_retrieval_fixture(tmp_path: Path, source: str) -> tuple[Path, Path]:
    package_root = tmp_path / "src"
    path = package_root / "kodocs" / "retrieval" / "probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    return path, package_root


def test_retrieval_package_exists() -> None:
    """Ensure the guarded package exists so the test cannot silently do nothing."""
    assert RETRIEVAL_PACKAGE.is_dir(), (
        f"retrieval package not found: {RETRIEVAL_PACKAGE}"
    )


def test_m5_guarded_package_directories_exist() -> None:
    """Ensure every package covered by the M5 no-LLM guard exists."""
    missing = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (RETRIEVAL_PACKAGE, TOOLS_PACKAGE, CHUNKING_PACKAGE, EVAL_PACKAGE)
        if not path.is_dir()
    ]
    assert not missing, "M5 guarded packages not found:\n" + "\n".join(missing)


def test_retrieval_does_not_import_langchain_or_langgraph() -> None:
    """Reject framework and higher-layer imports from all retrieval Python files."""
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in sorted(RETRIEVAL_PACKAGE.rglob("*.py"))
        for violation in find_forbidden_imports(path, SOURCE_ROOT)
    ]

    assert not violations, "Forbidden retrieval imports found:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize(
    ("source", "expected_module"),
    [
        pytest.param("import langchain_openai\n", "langchain_openai", id="langchain"),
        pytest.param(
            "from langgraph.graph import StateGraph\n",
            "langgraph.graph",
            id="langgraph",
        ),
        pytest.param(
            "from ..agent import answer\n", "kodocs.agent", id="relative-module"
        ),
        pytest.param("from .. import agent\n", "kodocs.agent", id="relative-alias"),
        pytest.param(
            "from ...kodocs import ui\n", "kodocs.ui", id="over-deep-relative"
        ),
        pytest.param("from kodocs import agent\n", "kodocs.agent", id="absolute-alias"),
        pytest.param(
            "import kodocs.mcp_server.foo\n",
            "kodocs.mcp_server.foo",
            id="absolute-module",
        ),
        pytest.param(
            'import importlib\nimportlib.import_module("kodocs.agent")\n',
            "kodocs.agent",
            id="importlib-literal",
        ),
        pytest.param('__import__("kodocs.ui")\n', "kodocs.ui", id="builtin-literal"),
        pytest.param(
            "def load():\n    import kodocs.ui.views\n",
            "kodocs.ui.views",
            id="function-local",
        ),
        pytest.param("import kodocs.tools\n", "kodocs.tools", id="tools"),
    ],
)
def test_find_forbidden_imports_detects_each_violation_form(
    tmp_path: Path,
    source: str,
    expected_module: str,
) -> None:
    """Prove the retrieval guard catches every prohibited import form."""
    path, package_root = _write_retrieval_fixture(tmp_path, source)

    violations = find_forbidden_imports(path, package_root)

    assert any(item.endswith(f": {expected_module}") for item in violations)


def test_find_forbidden_imports_accepts_allowed_file(tmp_path: Path) -> None:
    """Prove ordinary imports and non-literal dynamic imports remain allowed."""
    path, package_root = _write_retrieval_fixture(
        tmp_path,
        "import importlib\n"
        "from pathlib import Path\n"
        "from kodocs.retrieval import authorized_search\n"
        "\n"
        "def load(module_name):\n"
        "    return importlib.import_module(module_name)\n",
    )

    assert find_forbidden_imports(path, package_root) == []


def test_tools_package_exists() -> None:
    """Ensure the tools boundary cannot silently scan an absent package."""
    assert TOOLS_PACKAGE.is_dir(), f"tools package not found: {TOOLS_PACKAGE}"


def test_tools_package_respects_layer_boundary() -> None:
    """Keep orchestration, MCP, UI, LLM, and framework imports out of tools."""
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in sorted(TOOLS_PACKAGE.rglob("*.py"))
        for violation in find_forbidden_imports(
            path,
            SOURCE_ROOT,
            TOOLS_FORBIDDEN_IMPORTS,
        )
    ]

    assert not violations, "Forbidden tools imports found:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "expected_module"),
    [
        pytest.param("import kodocs.agent\n", "kodocs.agent", id="agent"),
        pytest.param(
            "import kodocs.mcp_server\n", "kodocs.mcp_server", id="mcp-server"
        ),
        pytest.param("import kodocs.ui\n", "kodocs.ui", id="ui"),
        pytest.param("import kodocs.llm\n", "kodocs.llm", id="llm"),
        pytest.param("import langchain_openai\n", "langchain_openai", id="langchain"),
        pytest.param("import langgraph.graph\n", "langgraph.graph", id="langgraph"),
        pytest.param("import mcp\n", "mcp", id="mcp"),
        pytest.param("import mcp.server\n", "mcp.server", id="mcp-child"),
        pytest.param("import openai\n", "openai", id="openai"),
        pytest.param("import anthropic\n", "anthropic", id="anthropic"),
    ],
)
def test_tools_guard_detects_forbidden_imports(
    tmp_path: Path,
    source: str,
    expected_module: str,
) -> None:
    """Prove every prohibited tools dependency is detected by the AST guard."""
    path, package_root = _write_retrieval_fixture(tmp_path, source)

    violations = find_forbidden_imports(
        path,
        package_root,
        TOOLS_FORBIDDEN_IMPORTS,
    )

    assert any(item.endswith(f": {expected_module}") for item in violations)


@pytest.mark.parametrize("package", [AGENT_PACKAGE, MCP_SERVER_PACKAGE])
def test_retrieval_consumers_do_not_import_retrieval(package: Path) -> None:
    """Require agent and MCP server code to call retrieval only through tools."""
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in sorted(package.rglob("*.py"))
        for violation in find_forbidden_imports(
            path,
            SOURCE_ROOT,
            RETRIEVAL_CONSUMER_FORBIDDEN_IMPORTS,
        )
    ]

    assert not violations, f"Direct retrieval imports found in {package.name}:\n" + (
        "\n".join(violations)
    )


@pytest.mark.parametrize("package_name", ["agent", "mcp_server"])
def test_retrieval_consumer_guard_detects_violation(
    tmp_path: Path,
    package_name: str,
) -> None:
    """Prove both retrieval consumer guards reject direct imports."""
    package_root = tmp_path / "src"
    path = package_root / "kodocs" / package_name / "probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from kodocs.retrieval import authorized_search\n", encoding="utf-8"
    )

    violations = find_forbidden_imports(
        path,
        package_root,
        RETRIEVAL_CONSUMER_FORBIDDEN_IMPORTS,
    )

    assert any(item.endswith(": kodocs.retrieval") for item in violations)


@pytest.mark.parametrize(
    ("source", "expected_module"),
    [
        pytest.param("import anthropic\n", "anthropic", id="anthropic"),
        pytest.param("from openai import OpenAI\n", "openai", id="openai"),
        pytest.param("import google.genai\n", "google.genai", id="google-genai"),
        pytest.param("from kodocs import llm\n", "kodocs.llm", id="kodocs-llm"),
    ],
)
def test_m5_llm_guard_detects_forbidden_imports(
    tmp_path: Path,
    source: str,
    expected_module: str,
) -> None:
    """Prove every prohibited LLM namespace is recognized by the M5 guard."""
    path, package_root = _write_retrieval_fixture(tmp_path, source)

    violations = _find_imports_matching(
        path,
        package_root,
        M5_LLM_FORBIDDEN_IMPORTS,
    )

    assert any(item.endswith(f": {expected_module}") for item in violations)


def test_m5_modules_do_not_import_llm_clients() -> None:
    """Reject LLM imports in M5 code; future eval modules must start with ``m5_``."""
    target_files = sorted(
        {
            *RETRIEVAL_PACKAGE.rglob("*.py"),
            *TOOLS_PACKAGE.rglob("*.py"),
            *CHUNKING_PACKAGE.rglob("*.py"),
            *EVAL_PACKAGE.glob("m5_*.py"),
        }
    )
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in target_files
        for violation in _find_imports_matching(
            path,
            SOURCE_ROOT,
            M5_LLM_FORBIDDEN_IMPORTS,
        )
    ]

    assert not violations, "LLM imports found in deterministic M5 code:\n" + "\n".join(
        violations
    )
