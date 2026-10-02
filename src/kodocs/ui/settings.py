"""UI-only answer settings, kept apart from the frozen M6/M7 evaluation settings."""

from dataclasses import replace

from kodocs.llm.answer_settings import AnswerModelConfig

# Evidence: the largest successful answer in the cost ledger is 780 output
# tokens, while a runaway generation waits until the 4000-token cap (~20-30 s).
UI_FINAL_MAX_OUTPUT_TOKENS = 1500


def ui_final_config(base: AnswerModelConfig) -> AnswerModelConfig:
    """Streaming final-answer settings of the GUI; tool-selection turns keep ``base``."""
    return replace(base, max_output_tokens=UI_FINAL_MAX_OUTPUT_TOKENS)


def ui_final_record(base: AnswerModelConfig) -> dict[str, object]:
    """Record with its own hash; never equal to the frozen M6/M7 settings hash."""
    config = ui_final_config(base)
    return {
        "id": "ui-final",
        "settings": config.record(),
        "settings_hash": config.settings_hash(),
        "derived_from_settings_hash": base.settings_hash(),
    }
