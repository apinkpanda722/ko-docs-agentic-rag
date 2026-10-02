"""UI-only output cap: separate record and hash, evaluation settings untouched."""

import json
from pathlib import Path
from unittest.mock import Mock

from kodocs.eval.m6_config import CONFIG_PATH, verify_config
from kodocs.llm.answer_settings import AnswerModelConfig
from kodocs.ui import service
from kodocs.ui.settings import (
    UI_FINAL_MAX_OUTPUT_TOKENS,
    ui_final_config,
    ui_final_record,
)

ROOT = Path(__file__).resolve().parents[2]


def frozen_model():
    frozen = json.loads((ROOT / CONFIG_PATH).read_text(encoding="utf-8"))
    return AnswerModelConfig(**frozen["answer"]["settings"]), frozen


def test_ui_cap_is_1500_and_everything_else_matches_the_frozen_settings():
    base, _ = frozen_model()
    ui = ui_final_config(base)
    assert UI_FINAL_MAX_OUTPUT_TOKENS == 1500 and ui.max_output_tokens == 1500
    assert base.max_output_tokens == 4000
    assert {k: v for k, v in ui.record().items() if k != "max_output_tokens"} == {
        k: v for k, v in base.record().items() if k != "max_output_tokens"
    }


def test_ui_record_has_its_own_hash_and_the_frozen_hashes_do_not_change():
    base, frozen = frozen_model()
    record = ui_final_record(base)
    assert record["settings_hash"] != base.settings_hash()
    assert record["derived_from_settings_hash"] == base.settings_hash()
    assert base.settings_hash() == frozen["answer"]["settings_hash"]
    assert verify_config(ROOT / CONFIG_PATH).ok  # m6_config still verifies
    m7 = json.loads((ROOT / "data/manifest/m7_config.json").read_text())
    assert json.dumps(m7).count(record["settings_hash"]) == 0


def test_service_derives_the_ui_chat_only_when_the_chat_supports_it():
    plain = object()
    assert service._final_chat(plain) is plain  # fakes keep working
    derived = Mock()
    chat = Mock()
    chat.with_max_output_tokens.return_value = derived
    assert service._final_chat(chat) is derived
    chat.with_max_output_tokens.assert_called_once_with(UI_FINAL_MAX_OUTPUT_TOKENS)
