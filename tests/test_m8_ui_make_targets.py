"""Keep the verification entry point and UI launch command explicit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_verify_includes_ui_group_without_changing_ruff():
    source = (ROOT / "Makefile").read_text()
    recipe = source.split("verify:\n", 1)[1].split("\n\n", 1)[0]
    assert recipe.splitlines() == [
        "\tuv run ruff check .",
        "\tuv run ruff format --check .",
        "\tuv run --group ui pytest -q",
    ]


def test_ui_target_is_declared_for_user_execution():
    source = (ROOT / "Makefile").read_text()
    assert "ui" in source.splitlines()[0].split()[1:]
    assert "ui:\n\tuv run --group ui streamlit run src/kodocs/ui/app.py\n" in source
    assert "make ui  # Streamlit 데모 GUI 기동" in (ROOT / "AGENTS.md").read_text()
