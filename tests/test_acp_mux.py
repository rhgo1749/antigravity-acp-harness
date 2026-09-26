"""Routing/health tests for scripts/acp_mux.py — no live credentials required."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acp_mux", ROOT / "scripts" / "acp_mux.py")
assert spec is not None and spec.loader is not None
acp_mux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acp_mux)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_mux, "REGISTRY_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setattr(acp_mux, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(acp_mux, "LOG_PATH", str(tmp_path / "mux.log"))
    monkeypatch.setattr(acp_mux, "DEFAULT_HOME", str(tmp_path / "default-home"))
    monkeypatch.setattr(acp_mux, "AUTH_COOLDOWN", 300.0)
    monkeypatch.setattr(acp_mux, "QUOTA_COOLDOWN", 900.0)
    monkeypatch.setattr(acp_mux, "CRASH_COOLDOWN", 60.0)
    (tmp_path / "default-home").mkdir()
    return tmp_path


def _registry(env: Path, accounts: list[dict]) -> None:
    Path(acp_mux.REGISTRY_PATH).write_text(json.dumps({"accounts": accounts}))


def _health_key(label: str, home: str) -> str:
    return f"{label}\t{home}"


def test_no_registry_falls_back_to_default_home(env):
    home, label, reason = acp_mux._pick_account(now=100.0)
    assert reason == "no-registry"
    assert home == str(env / "default-home")
    assert label == "default"


def test_control_home_prefers_explicit_then_hermes_real_home():
    assert acp_mux._control_home({
        "ACP_MUX_HOME": "/mux-control",
        "HERMES_REAL_HOME": "/real-home",
        "HOME": "/profile-home",
    }) == "/mux-control"
    assert acp_mux._control_home({
        "HERMES_REAL_HOME": "/real-home",
        "HOME": "/profile-home",
    }) == "/real-home"
    assert acp_mux._control_home({"HOME": "/profile-home"}) == "/profile-home"


def test_imported_default_home_uses_hermes_real_home(tmp_path):
    real_home = tmp_path / "real-home"
    profile_home = tmp_path / "profile-home"
    real_home.mkdir()
    profile_home.mkdir()
    proc_env = os.environ.copy()
    proc_env.update({
        "HOME": str(profile_home),
        "HERMES_REAL_HOME": str(real_home),
    })
    proc_env.pop("ACP_MUX_HOME", None)
    module_path = ROOT / "scripts" / "acp_mux.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util; "
                f"s=importlib.util.spec_from_file_location('m',{str(module_path)!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.DEFAULT_HOME)"
            ),
        ],
        text=True,
        capture_output=True,
        env=proc_env,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(real_home)


def test_round_robin_within_same_priority_tier(env):
    a = str(env / "a")
    b = str(env / "b")
    _registry(env, [
        {"home": a, "label": "a", "priority": 0},
        {"home": b, "label": "b", "priority": 0},
    ])

    assert acp_mux._pick_account(now=100.0)[1:] == ("a", "round-robin")
    assert acp_mux._pick_account(now=100.0)[1:] == ("b", "round-robin")
    assert acp_mux._pick_account(now=100.0)[1:] == ("a", "round-robin")


def test_lower_priority_is_used_when_preferred_tier_is_quarantined(env, monkeypatch):
    a = str(env / "a")
    b = str(env / "b")
    _registry(env, [
        {"home": a, "label": "a", "priority": 0},
        {"home": b, "label": "b", "priority": 1},
    ])
    monkeypatch.setattr(acp_mux.time, "time", lambda: 100.0)
    acp_mux._mark_failure(a, "a", "quota", "429 quota exhausted")

    home, label, reason = acp_mux._pick_account(now=101.0)
    assert (home, label, reason) == (b, "b", "priority")


def test_all_quarantined_probes_earliest_recovery(env):
    a = str(env / "a")
    b = str(env / "b")
    _registry(env, [
        {"home": a, "label": "a", "priority": 0},
        {"home": b, "label": "b", "priority": 0},
    ])
    state = acp_mux._empty_state()
    state["accounts"] = {
        _health_key("a", a): {"cooldown_until": 500.0},
        _health_key("b", b): {"cooldown_until": 300.0},
    }
    Path(acp_mux.STATE_PATH).write_text(json.dumps(state))

    home, label, reason = acp_mux._pick_account(now=200.0)
    assert (home, label, reason) == (b, "b", "cooldown-probe")


def test_failure_classification_is_strong_and_does_not_scan_normal_content():
    assert acp_mux._classify_failure("HTTP 429 RESOURCE_EXHAUSTED") == "quota"
    assert acp_mux._classify_failure("rate limit exceeded") == "quota"
    assert acp_mux._classify_failure("401 unauthenticated") == "auth"
    assert acp_mux._classify_failure("refresh token revoked") == "auth"
    assert acp_mux._classify_failure("ordinary model response about quotas") is None

    normal = json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"update": {"content": "429 quota exceeded is an example"}},
    })
    assert acp_mux._classify_json_error(normal) is None

    error = json.dumps({"jsonrpc": "2.0", "id": 7, "error": {"code": 429, "message": "quota exceeded"}})
    assert acp_mux._classify_json_error(error) == ("quota", '{"code": 429, "message": "quota exceeded"}')


def test_mark_failure_persists_cooldown_without_vendor_credentials(env, monkeypatch):
    home = str(env / "account")
    monkeypatch.setattr(acp_mux.time, "time", lambda: 100.0)
    acp_mux._mark_failure(home, "account", "auth", "401 unauthenticated")

    state = json.loads(Path(acp_mux.STATE_PATH).read_text())
    record = state["accounts"][_health_key("account", home)]
    assert record["reason"] == "auth"
    assert record["cooldown_until"] == 400.0
    assert record["failures"] == 1


def test_duplicate_failure_lines_do_not_extend_same_event(env, monkeypatch):
    home = str(env / "account")
    times = iter([100.0, 102.0])
    monkeypatch.setattr(acp_mux.time, "time", lambda: next(times))

    acp_mux._mark_failure(home, "account", "quota", "429")
    acp_mux._mark_failure(home, "account", "quota", "RESOURCE_EXHAUSTED")

    state = json.loads(Path(acp_mux.STATE_PATH).read_text())
    record = state["accounts"][_health_key("account", home)]
    assert record["cooldown_until"] == 1000.0
    assert record["failures"] == 1


def test_secret_redaction_never_leaks_tokens():
    line = "refresh_token=0Aabcdef... ya29.sometokenhere"
    out = acp_mux._redact(line)
    assert "0Aabcdef" not in out
    assert "ya29.sometokenhere" not in out


def test_mux_has_no_direct_google_auth_or_internal_quota_client():
    source = (ROOT / "scripts" / "acp_mux.py").read_text(encoding="utf-8")
    assert "cloudcode-pa.googleapis.com" not in source
    assert "retrieveUserQuotaSummary" not in source
    assert "urllib.request" not in source
    assert "acp_token.json" not in source


def test_protocol_relay_with_fake_official_acp(tmp_path):
    fake = tmp_path / "fake_acp.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':request.get('id'),'result':{'ok':True}}), flush=True)\n"
    )
    fake.chmod(0o755)

    home = tmp_path / "account-home"
    home.mkdir()
    registry = tmp_path / "accounts.json"
    registry.write_text(json.dumps({"accounts": [{"home": str(home), "label": "fake", "priority": 0}]}))
    state = tmp_path / "state.json"
    log = tmp_path / "mux.log"

    proc_env = os.environ.copy()
    proc_env.update({
        "ACP_MUX_ACCOUNTS": str(registry),
        "ACP_MUX_STATE": str(state),
        "ACP_MUX_LOG": str(log),
        "ACP_MUX_PAR": str(fake),
    })
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "acp_mux.py")],
        input=request,
        text=True,
        capture_output=True,
        env=proc_env,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    reply = json.loads(completed.stdout.strip())
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert "selected fake" in log.read_text()
