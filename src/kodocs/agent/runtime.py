"""Public default runtime using the existing M6/M7 managed-chat construction."""

import json
from functools import lru_cache
from pathlib import Path

from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.types import AgentRuntime
from kodocs.config import load_settings
from kodocs.costs import DEFAULT_LEDGER_PATH
from kodocs.llm.answer_settings import AnswerModelConfig
from kodocs.llm.openai_chat import ManagedChat
from kodocs.tools import build_tool_deps
from kodocs.tools.deps import M5_CONFIG_HASH


@lru_cache(maxsize=1)
def build_default_runtime() -> AgentRuntime:
    """Prepare local tools once, with the frozen M6 settings and real settings loader."""
    settings = load_settings()
    settings.require_key("OPENAI_API_KEY")
    frozen = json.loads(
        Path("data/manifest/m6_config.json").read_text(encoding="utf-8")
    )
    if frozen["m5_config_hash"] != M5_CONFIG_HASH:
        raise ValueError("M5 configuration mismatch")
    model = AnswerModelConfig(**frozen["answer"]["settings"])
    guardrails = GuardrailConfig(**frozen["guardrails"]["settings"])
    chat = ManagedChat(
        model, settings, ledger_path=DEFAULT_LEDGER_PATH, run_id="streamlit-ui"
    )
    return AgentRuntime(
        deps=build_tool_deps(),
        chat=chat,
        model_record=model.record() | {"settings_hash": model.settings_hash()},
        m5_config_hash=M5_CONFIG_HASH,
        guardrails=guardrails,
        trace_dir=Path("logs/traces"),
    )
