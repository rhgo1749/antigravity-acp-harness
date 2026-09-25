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

from pathlib import Path

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