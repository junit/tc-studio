import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_ollama_official_bypass_never_skips_local_port_validation():
    from apis.agent import _is_official_model_base_url

    assert _is_official_model_base_url("ollama", "http://localhost:6379") is False
    assert _is_official_model_base_url("ollama", "http://localhost:11434") is False


def test_non_admin_can_probe_allowed_local_ollama_without_key():
    from unittest.mock import MagicMock, patch

    from apis.agent import FetchModelsPayload, fetch_available_models
    from core.auth import CurrentUser

    response = MagicMock(status_code=200)
    response.json.return_value = {"models": [{"name": "llama3"}]}
    with patch("apis.agent.requests.get", return_value=response):
        result = fetch_available_models(
            FetchModelsPayload(provider="ollama", base_url="http://localhost:11434"),
            CurrentUser("user-1"),
        )
    assert result["source"] == "remote"
    assert result["models"] == ["llama3"]


def test_persistence_rejects_private_model_base_url():
    """BOT 持久化层必须拦截内网模型地址，不得仅在 /models 探测时校验。"""
    import pytest
    from fastapi import HTTPException

    from apis.agent import _validate_model_override_for_persistence
    from core.auth import CurrentUser

    override = {"provider": "custom", "base_url": "http://10.0.0.5:8000/v1", "api_key": "sk-user"}
    with pytest.raises(HTTPException) as exc_info:
        _validate_model_override_for_persistence(override, CurrentUser("user-1", frozenset({"operator"})))
    assert exc_info.value.status_code == 422


def _fake_public_getaddrinfo(host, port=None, **_kwargs):
    """沙箱内无真实 DNS；统一伪装解析到公网地址以测策略逻辑。"""
    return [(2, 1, 6, "", ("93.184.216.34", port or 443))]


def test_persistence_non_admin_custom_url_requires_own_api_key(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from apis.agent import _validate_model_override_for_persistence
    from core.auth import CurrentUser

    class _Cfg:
        provider = "deepseek"

    import apis.agent as agent_api

    monkeypatch.setattr("core.agent.model_url_guard.socket.getaddrinfo", _fake_public_getaddrinfo)
    monkeypatch.setattr(agent_api.bot_manager, "get_llm_config", lambda: _Cfg())
    override = {"provider": "custom", "base_url": "https://relay.example.com/v1"}
    with pytest.raises(HTTPException) as exc_info:
        _validate_model_override_for_persistence(override, CurrentUser("user-1", frozenset({"operator"})))
    assert exc_info.value.status_code == 403

    override_with_key = {"provider": "custom", "base_url": "https://relay.example.com/v1", "api_key": "sk-user"}
    assert _validate_model_override_for_persistence(override_with_key, CurrentUser("user-1", frozenset({"operator"}))) is override_with_key


def test_persistence_non_admin_official_or_local_url_without_key_allowed(monkeypatch):
    from apis.agent import _validate_model_override_for_persistence
    from core.auth import CurrentUser

    monkeypatch.setattr("core.agent.model_url_guard.socket.getaddrinfo", _fake_public_getaddrinfo)

    official = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1"}
    assert _validate_model_override_for_persistence(official, CurrentUser("user-1", frozenset({"operator"}))) is official

    local = {"provider": "ollama", "base_url": "http://localhost:11434"}
    assert _validate_model_override_for_persistence(local, CurrentUser("user-1", frozenset({"operator"}))) is local


def test_engine_create_model_rejects_private_base_url():
    """实际出站构造路径（engine.create_model）也必须拦截内网地址。"""
    import pytest

    from core.agent.engine import agent_engine
    from core.agent.models import LLMConfig

    cfg = LLMConfig(provider="custom", model_name="m", base_url="http://169.254.169.254/v1", api_key="k")
    with pytest.raises(ValueError, match="安全策略拒绝"):
        agent_engine.create_model(cfg)


def test_configured_private_ip_is_allowed_only_when_explicitly_allowlisted(monkeypatch):
    import pytest

    from core.agent.model_url_guard import UnsafeModelUrlError, validate_model_base_url

    url = "http://10.0.0.5:11434/v1"
    with pytest.raises(UnsafeModelUrlError):
        validate_model_base_url(url)

    monkeypatch.setenv("ALLOW_PRIVATE_MODEL_HOSTS", "10.0.0.5")
    assert validate_model_base_url(url) == url


def test_configured_private_dns_host_requires_private_resolution(monkeypatch):
    import pytest

    from core.agent.model_url_guard import UnsafeModelUrlError, validate_model_base_url

    monkeypatch.setattr(
        "core.agent.model_url_guard.socket.getaddrinfo",
        lambda host, port=None, **_: [(2, 1, 6, "", ("192.168.20.10", port or 8000))],
    )
    url = "http://gpu.internal:8000/v1"
    with pytest.raises(UnsafeModelUrlError):
        validate_model_base_url(url)

    monkeypatch.setenv("ALLOW_PRIVATE_MODEL_HOSTS", "gpu.internal")
    assert validate_model_base_url(url) == url


def test_private_host_allowlist_supports_cidr_without_allowing_public_dns(monkeypatch):
    import pytest

    from core.agent.model_url_guard import UnsafeModelUrlError, validate_model_base_url

    monkeypatch.setenv("ALLOW_PRIVATE_MODEL_HOSTS", "192.168.20.0/24")
    assert validate_model_base_url("http://192.168.20.10:8000/v1").endswith("/v1")

    with pytest.raises(UnsafeModelUrlError):
        validate_model_base_url("http://192.168.21.10:8000/v1")

    monkeypatch.setattr(
        "core.agent.model_url_guard.socket.getaddrinfo",
        lambda host, port=None, **_: [(2, 1, 6, "", ("192.168.20.11", port or 8000))],
    )
    with pytest.raises(UnsafeModelUrlError):
        validate_model_base_url("http://unlisted.internal:8000/v1")


def test_malformed_model_url_is_rejected_as_policy_error():
    import pytest

    from core.agent.model_url_guard import UnsafeModelUrlError, validate_model_base_url

    with pytest.raises(UnsafeModelUrlError):
        validate_model_base_url("http://gpu.internal:not-a-port/v1")
