from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_memory_and_guideline_payloads_reject_unbounded_text():
    from apis.agent import GuidelinePayload, MemoryPayload

    with pytest.raises(ValidationError):
        MemoryPayload(key="x", content="x" * 16_385)
    with pytest.raises(ValidationError):
        GuidelinePayload(instruction="x" * 16_385)
    with pytest.raises(ValidationError):
        GuidelinePayload(instruction="ok", reason="x" * 4_097)


def test_bot_payload_rejects_large_prompt_and_model_override():
    from apis.agent import CreateBotPayload, UpdateBotPayload

    with pytest.raises(ValidationError):
        CreateBotPayload(name="bot", base_prompt="x" * 65_537)
    with pytest.raises(ValidationError):
        UpdateBotPayload(model_override={"payload": "x" * (32 * 1024)})


def test_knowledge_query_and_top_k_are_bounded():
    from apis.agent import SearchKnowledgePayload

    with pytest.raises(ValidationError):
        SearchKnowledgePayload(query="x" * 4_097)
    with pytest.raises(ValidationError):
        SearchKnowledgePayload(query="ok", top_k=51)
