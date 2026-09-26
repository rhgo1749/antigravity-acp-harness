"""Static guards for the Hermes provider profile.

The plugin module imports Hermes' provider registry, which is unavailable in
this repo's test environment, so these assertions read the source instead.
They pin the two deployment regressions observed on 2026-09-25:

1. ``process_args`` must stay non-empty. CopilotACPClient substitutes its
   default ``--acp --stdio`` for falsy args; the official
   ``agy_acp_server.par`` rejects ``--acp`` (FATAL unknown flag), fetch_models
   swallows the failure, and the model picker silently degrades.
2. ``fallback_models`` must stay empty. The old ``("antigravity-acp",)`` stub
   was a transport label, not a model, and showed up as a phantom picker
   entry whenever the live catalog fetch failed.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "hermes-plugin"
    / "antigravity-acp"
    / "__init__.py"
).read_text(encoding="utf-8")


def test_process_args_pin_official_wrapper_contract() -> None:
    assert 'process_args=("--uid=",)' in PLUGIN_SOURCE
    assert "process_args=()," not in PLUGIN_SOURCE


def test_no_phantom_stub_fallback_model() -> None:
    assert 'fallback_models=("antigravity-acp",)' not in PLUGIN_SOURCE
    assert "fallback_models=()," in PLUGIN_SOURCE


def _load_plugin(monkeypatch, client_cls):
    registered = []

    class FakeProviderProfile:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    providers = types.ModuleType("providers")
    providers.register_provider = registered.append
    providers_base = types.ModuleType("providers.base")
    providers_base.ProviderProfile = FakeProviderProfile
    agent = types.ModuleType("agent")
    agent.__path__ = []
    acp_client = types.ModuleType("agent.copilot_acp_client")
    acp_client.CopilotACPClient = client_cls

    monkeypatch.setitem(sys.modules, "providers", providers)
    monkeypatch.setitem(sys.modules, "providers.base", providers_base)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.copilot_acp_client", acp_client)

    path = (
        Path(__file__).resolve().parent.parent
        / "hermes-plugin"
        / "antigravity-acp"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("antigravity_acp_plugin_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert registered == [module.antigravity_acp]
    return module


def test_rate_limit_retry_reopens_session_until_success(monkeypatch) -> None:
    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def _run_prompt(self, prompt_text, *, timeout_seconds, model=None):
            FakeClient.calls += 1
            if FakeClient.calls < 3:
                raise RuntimeError("ACP session/prompt failed: 429 RESOURCE_EXHAUSTED")
            return "ok", ""

    monkeypatch.delenv("HERMES_ANTIGRAVITY_ACP_RATE_LIMIT_RETRIES", raising=False)
    module = _load_plugin(monkeypatch, FakeClient)
    client = module.antigravity_acp.create_client()

    assert client._run_prompt("hello", timeout_seconds=10) == ("ok", "")
    assert FakeClient.calls == 3


def test_non_rate_limit_error_is_not_retried(monkeypatch) -> None:
    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def _run_prompt(self, prompt_text, *, timeout_seconds, model=None):
            FakeClient.calls += 1
            raise RuntimeError("ACP session/prompt failed: invalid model")

    module = _load_plugin(monkeypatch, FakeClient)
    client = module.antigravity_acp.create_client()

    with pytest.raises(RuntimeError, match="invalid model"):
        client._run_prompt("hello", timeout_seconds=10)
    assert FakeClient.calls == 1


def test_rate_limit_retry_can_be_disabled(monkeypatch) -> None:
    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def _run_prompt(self, prompt_text, *, timeout_seconds, model=None):
            FakeClient.calls += 1
            raise RuntimeError("too many requests (429)")

    monkeypatch.setenv("HERMES_ANTIGRAVITY_ACP_RATE_LIMIT_RETRIES", "0")
    module = _load_plugin(monkeypatch, FakeClient)
    client = module.antigravity_acp.create_client()

    with pytest.raises(RuntimeError, match="429"):
        client._run_prompt("hello", timeout_seconds=10)
    assert FakeClient.calls == 1