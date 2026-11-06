"""Regression coverage for skill save size and static-code preflight checks."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from apis import agent as agent_api
from core.agent.bot_manager import BotManager
from core.agent.tools import MAX_SKILL_CODE_BYTES, SaveSkillTool


def test_skill_payload_rejects_code_over_64_kibibytes():
    with pytest.raises(ValidationError):
        agent_api.SaveSkillPayload(
            name="oversized",
            description="too large",
            # Keep the character count below 64 KiB while exceeding the byte
            # budget to verify the explicit UTF-8 bound.
            python_code="你" * (MAX_SKILL_CODE_BYTES // 3 + 1),
        )


def test_skill_endpoint_runs_ast_preflight_before_persisting(tmp_path, monkeypatch):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("skill_preflight", "Skill preflight")
    monkeypatch.setattr(agent_api, "bot_manager", manager)

    payload = agent_api.SaveSkillPayload(
        name="unsafe",
        description="must be rejected",
        python_code="import os\ndef execute():\n    return os.system('id')",
    )
    with pytest.raises(HTTPException) as exc_info:
        agent_api.create_or_save_skill(bot.id, payload)

    assert exc_info.value.status_code == 400
    assert "预检" in str(exc_info.value.detail)
    assert manager.get_skill(bot.id, "unsafe") is None


def test_save_skill_tool_runs_same_preflight(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("tool_preflight", "Skill tool preflight")
    tool = SaveSkillTool(bot.id, manager)

    result = tool.forward(
        "unsafe",
        "must be rejected",
        "import subprocess\ndef execute():\n    return 1",
    )

    assert "预检" in result
    assert manager.get_skill(bot.id, "unsafe") is None
