"""Account selection tests for scripts/acp_mux.py — no live credentials needed.

`_score_account` and `_pick_account` are exercised with monkeypatched network
calls and throwaway HOME dirs, per AGENTS.md (protocol behavior with fakes).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acp_mux", ROOT / "scripts" / "acp_mux.py")
assert spec is not None and spec.loader is not None
acp_mux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acp_mux)


def _make_home(tmp_path: Path, token: bool = True) -> str:
    home = tmp_path / f"home-{len(list(tmp_path.iterdir()))}"
    if token:
        tok = Path(home) / acp_mux._TOKEN_SUBPATH
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text(json.dumps({
            "client_id": "x", "client_secret": "y",
            "refresh_token": "z", "token_uri": "https://example.invalid/token",
        }))
    else:
        home.mkdir(parents=True, exist_ok=True)
    return str(home)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_mux, "REGISTRY_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setattr(acp_mux, "CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(acp_mux, "LOG_PATH", str(tmp_path / "mux.log"))
    monkeypatch.setattr(acp_mux, "DEFAULT_HOME", str(tmp_path / "default-home"))
    (tmp_path / "default-home").mkdir()
    return tmp_path


def test_no_registry_falls_back_to_default_home(env, monkeypatch):
    monkeypatch.setattr(acp_mux, "_access_token", lambda c: "t")
    monkeypatch.setattr(acp_mux, "_quota_score", lambda t: 0.5)
    home, label, reason = acp_mux._pick_account()
    assert reason == "no-registry"
    assert home == str(env / "default-home")


def test_picks_highest_remaining_fraction(env, monkeypatch):
    homes = {
        "low": _make_home(env),
        "high": _make_home(env),
    }
    scores = {}

    def fake_score(token):
        return scores[token]

    monkeypatch.setattr(acp_mux, "_access_token", lambda c: c["refresh_token"])
    monkeypatch.setattr(acp_mux, "_quota_score", fake_score)
    registry = {"accounts": [
        {"home": homes["low"], "label": "low", "priority": 0},
        {"home": homes["high"], "label": "high", "priority": 1},
    ]}
    Path(acp_mux.REGISTRY_PATH).write_text(json.dumps(registry))
    scores["z"] = 0.9  # both accounts share the same fake token value initially
    # differentiate by read order: patch score per call sequence instead
    sequence = iter([0.2, 0.9])
    monkeypatch.setattr(acp_mux, "_quota_score", lambda t: next(sequence))
    home, label, reason = acp_mux._pick_account()
    assert (label, reason) == ("high", "quota-best")


def test_tie_break_uses_priority(env, monkeypatch):
    homes = [_make_home(env), _make_home(env)]
    monkeypatch.setattr(acp_mux, "_access_token", lambda c: "t")
    monkeypatch.setattr(acp_mux, "_quota_score", lambda t: 0.5)
    Path(acp_mux.REGISTRY_PATH).write_text(json.dumps({"accounts": [
        {"home": homes[1], "label": "second", "priority": 5},
        {"home": homes[0], "label": "first", "priority": 1},
    ]}))
    home, label, reason = acp_mux._pick_account()
    assert (label, reason) == ("first", "quota-best")


def test_unusable_credentials_excluded_from_fallback(env, monkeypatch):
    good = _make_home(env)
    broken = _make_home(env, token=False)
    Path(acp_mux.REGISTRY_PATH).write_text(json.dumps({"accounts": [
        {"home": broken, "label": "broken", "priority": 0},
        {"home": good, "label": "good", "priority": 1},
    ]}))
    monkeypatch.setattr(acp_mux, "_access_token", lambda c: "t")
    monkeypatch.setattr(acp_mux, "_quota_score", lambda t: None)  # quota silent
    home, label, reason = acp_mux._pick_account()
    # broken has no credential file at all; good scores 'quota-empty' (usable)
    assert (label, reason) == ("good", "priority-fallback")


def test_auth_penalty_shields_revoked_account(env, monkeypatch):
    home = _make_home(env)
    Path(acp_mux.REGISTRY_PATH).write_text(json.dumps({"accounts": [
        {"home": home, "label": "revoked", "priority": 0},
    ]}))
    import urllib.error

    def http_401(*a, **k):
        raise urllib.error.HTTPError("https://x", 401, "no", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(acp_mux, "_access_token", http_401)
    acp_mux._pick_account()  # sets penalty
    home2, label, reason = acp_mux._pick_account()
    assert reason == "no-usable-credentials"
    cache = json.loads(Path(acp_mux.CACHE_PATH).read_text())
    assert cache["revoked"]["penalty_until"] > 0


def test_secret_redaction_never_leaks_tokens():
    line = "refresh_token=0Aabcdef... ya29.sometokenhere"
    out = acp_mux._redact(line)
    assert "0Aabcdef" not in out
    assert "ya29.sometokenhere" not in out


def test_registry_absent_returns_empty_list(env):
    assert acp_mux._load_registry() == []