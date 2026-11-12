"""Exercise the installed model adapter, without real provider credentials/I/O."""

from __future__ import annotations

import json
import builtins

import httpx
import pytest

from core.agent.engine import RobustStreamingOpenAIServerModel
from core.agent.model_runtime import check_model_runtime


def test_real_model_adapter_initializes_without_provider_request(monkeypatch):
    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("Model construction must not contact a provider")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_request)
    model = RobustStreamingOpenAIServerModel(
        model_id="runtime-check",
        api_base="https://model-runtime.invalid/v1",
        api_key="synthetic-runtime-check",
    )
    model.client.close()


def test_model_startup_check_initializes_and_closes_real_client(monkeypatch):
    import openai

    closed = []
    close_client = openai.OpenAI.close

    def close(client):
        closed.append(client)
        close_client(client)

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("Startup check must not contact a provider")

    monkeypatch.setattr(openai.OpenAI, "close", close)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_request)
    check_model_runtime()
    assert len(closed) == 1
    assert closed[0].is_closed()


def test_model_startup_check_rejects_missing_openai_extra(monkeypatch):
    original_import = builtins.__import__

    def missing_openai(name, *args, **kwargs):
        if name == "openai":
            raise ModuleNotFoundError("No module named 'openai'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_openai)
    with pytest.raises(RuntimeError, match=r"smolagents\[openai\].*uv sync --locked"):
        check_model_runtime()


def test_http_startup_refuses_missing_model_runtime(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient

    def missing_runtime():
        raise RuntimeError("synthetic missing model runtime")

    monkeypatch.setattr(app_module, "check_model_runtime", missing_runtime)
    monkeypatch.setattr(app_module, "check_pdf_runtime", lambda: None)
    with pytest.raises(RuntimeError, match="synthetic missing model runtime"):
        with TestClient(app_module.app):
            pytest.fail("HTTP server must refuse to start without its model runtime")


def test_real_model_adapter_streams_openai_compatible_response():
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        chunks = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "分析"}}]},
            {"choices": [{"index": 0, "delta": {"content": "检查通过"}}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        model = RobustStreamingOpenAIServerModel(
            model_id="runtime-check",
            api_base="https://model-runtime.invalid/v1",
            api_key="synthetic-runtime-check",
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        deltas = list(model.generate_stream([{"role": "user", "content": "check"}]))
    assert "".join(delta.content or "" for delta in deltas) == "<think>分析</think>检查通过"
    assert deltas[-1].token_usage.input_tokens == 2
    assert deltas[-1].token_usage.output_tokens == 3
    assert len(requests) == 1
    assert requests[0]["stream"] is True
    assert requests[0]["model"] == "runtime-check"
