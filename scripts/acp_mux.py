#!/usr/bin/env python3
"""Antigravity ACP account multiplexer — one ACP endpoint, many Google accounts.

A client (e.g. Hermes' external_process provider) spawns this as a single ACP
v1 stdio agent. Per spawn (= per client session/request) it:

  1. loads the account registry  <HERMES_HOME>/acp-accounts.json  (hot-reload:
     add/remove accounts by editing this file only — no code change)
  2. queries each account's live quota via retrieveUserQuotaSummary
     (refresh-token flow; results cached ~60s on disk)
  3. picks the account with the best remaining quota (minimum
     remainingFraction across all windows, conservative), tie-break: lower
     registry priority
  4. spawns the official agy_acp_server.par under that account's HOME and
     relays JSON-RPC lines both ways

Degradation order: quota-scoreable accounts > priority-order accounts with
usable credentials > default HOME (legacy single-account behaviour).
Auth-failed accounts get a timed penalty so a revoked account doesn't slow
every spawn.

Accounts are isolated purely by HOME: each account's own
``$HOME/.gemini/antigravity-acp/acp_token.json`` is created by the official
server's own OAuth flow (spawn the server with HOME=<account home> and
complete `authenticate` in a browser). This program never writes, copies, or
mints OAuth tokens — it only reads the credential the official server already
owns in order to call the read-only quota endpoint, and tokens never leave
process memory or appear in logs.

Secrets: OAuth tokens exist in memory only. Relay stderr is redacted; the
selection log records labels/scores, never tokens.

Environment:
  ACP_MUX_ACCOUNTS   registry path (default ~/.hermes/acp-accounts.json)
  ACP_MUX_PAR        official agy_acp_server.par path (required)
  ACP_MUX_CACHE      quota cache path (default ~/.hermes/cache/acp-mux-quota.json)
  ACP_MUX_LOG        selection log path   (default ~/.hermes/logs/acp-mux.log)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REGISTRY_PATH = os.path.expanduser(os.environ.get("ACP_MUX_ACCOUNTS", "~/.hermes/acp-accounts.json"))
CACHE_PATH = os.path.expanduser(os.environ.get("ACP_MUX_CACHE", "~/.hermes/cache/acp-mux-quota.json"))
LOG_PATH = os.path.expanduser(os.environ.get("ACP_MUX_LOG", "~/.hermes/logs/acp-mux.log"))
PAR = os.environ.get("ACP_MUX_PAR", "")
QUOTA_TTL = 60.0
AUTH_PENALTY = 300.0
DEFAULT_HOME = os.environ.get("HOME", os.path.expanduser("~"))

_TOKEN_SUBPATH = os.path.join(".gemini", "antigravity-acp", "acp_token.json")


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _redact(line: str) -> str:
    line = re.sub(r"(access_token|refresh_token|id_token|code_verifier|client_secret)(=|:)\s*\S+", r"\1\2***", line, flags=re.I)
    line = re.sub(r"ya29\.\S+", "ya29.***", line)
    return line[:500]


def _load_registry() -> list[dict]:
    """[{home, label, priority}] — absent registry => empty (default-HOME fallback)."""
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, list):
            return [a for a in accounts if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _access_token(creds: dict) -> str | None:
    """Exchange the account's refresh token for a fresh access token (read-only)."""
    body = urllib.parse.urlencode(
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(creds["token_uri"], data=body, method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.load(resp)
    token = payload.get("access_token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


def _quota_score(access_token: str) -> float | None:
    """Conservative score: minimum remainingFraction across all windows."""
    req = urllib.request.Request(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": "Antigravity/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.load(resp)
    fractions: list[float] = []
    for group in payload.get("groups") or []:
        for bucket in group.get("buckets") or []:
            frac = bucket.get("remainingFraction")
            if isinstance(frac, (int, float)):
                fractions.append(float(frac))
    return min(fractions) if fractions else None


def _score_account(entry: dict, cache: dict) -> tuple[float | None, bool, str]:
    """Return (score, credential_usable, note).

    score None = not scoreable. credential_usable False = the ACP spawn under
    this HOME would fail auth too, so exclude it from priority fallback.
    """
    home = entry.get("home") or DEFAULT_HOME
    label = str(entry.get("label") or os.path.basename(home.rstrip("/")) or "acp")
    now = time.time()
    cached = cache.get(label)
    if isinstance(cached, dict):
        penalty_until = cached.get("penalty_until", 0)
        if penalty_until and now < penalty_until:
            return None, False, "penalized"
        if isinstance(cached.get("score"), (int, float)) and now - cached.get("at", 0) < QUOTA_TTL:
            return float(cached["score"]), True, "cached"
    path = os.path.join(home, _TOKEN_SUBPATH)
    try:
        with open(path, encoding="utf-8") as fh:
            creds = json.load(fh)
        token = _access_token(creds)
        if not token:
            return None, False, "token-exchange-failed"
        score = _quota_score(token)
        if score is None:
            # credential fine, quota API silent — spawn should still work
            return None, True, "quota-empty"
        cache[label] = {"score": score, "at": now, "penalty_until": 0}
        return score, True, "live"
    except urllib.error.HTTPError as exc:
        cache[label] = {"score": None, "at": now,
                        "penalty_until": now + AUTH_PENALTY if exc.code in (400, 401, 403) else 0}
        return None, False, f"http-{exc.code}"
    except (OSError, json.JSONDecodeError, urllib.error.URLError, KeyError, ValueError):
        return None, False, "credential-unreadable"


def _pick_account() -> tuple[str, str, str]:
    """Return (home, label, reason). Tiers: quota-scored > credential-usable > default."""
    accounts = _load_registry()
    if not accounts:
        return DEFAULT_HOME, "default", "no-registry"
    cache = _load_cache()
    scored: list[tuple[float, int, str, str]] = []
    usable: list[tuple[int, str, str]] = []
    for entry in accounts:
        priority = entry.get("priority")
        priority = priority if isinstance(priority, int) else 0
        score, cred_ok, note = _score_account(entry, cache)
        home = entry.get("home") or DEFAULT_HOME
        label = str(entry.get("label") or os.path.basename(home.rstrip("/")) or "acp")
        if score is not None:
            scored.append((score, priority, home, label))
        elif cred_ok:
            usable.append((priority, home, label))
        _log(f"candidate {label}: score={score if score is not None else 'n/a'} ({note})")
    _save_cache(cache)
    if scored:
        scored.sort(key=lambda t: (-t[0], t[1]))
        _, _, home, label = scored[0]
        return home, label, "quota-best"
    if usable:
        usable.sort(key=lambda t: t[0])
        _, home, label = usable[0]
        return home, label, "priority-fallback"
    return DEFAULT_HOME, "default", "no-usable-credentials"


def main() -> int:
    if not PAR or not os.path.exists(PAR):
        sys.stderr.write("antigravity-acp-mux: set ACP_MUX_PAR to the official agy_acp_server.par\n")
        return 2

    # Buffer stdin until the first request (initialize), then bind an upstream account.
    upstream: subprocess.Popen | None = None
    bound_label = ""

    def pump_upstream_out() -> None:
        assert upstream is not None
        for line in upstream.stdout or ():
            sys.stdout.write(line)
            sys.stdout.flush()

    def pump_upstream_err() -> None:
        assert upstream is not None
        for line in upstream.stderr or ():
            sys.stderr.write(_redact(line) + "\n")
            sys.stderr.flush()

    def bind(home: str, label: str, reason: str) -> bool:
        nonlocal upstream, bound_label
        env = dict(os.environ)
        env["HOME"] = home
        try:
            upstream = subprocess.Popen(
                [PAR, "--uid="],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, bufsize=1,
            )
        except OSError as exc:
            _log(f"spawn-failed {label}: {exc}")
            return False
        bound_label = label
        _log(f"bound account {label} ({reason}) pid={upstream.pid}")
        threading.Thread(target=pump_upstream_out, daemon=True).start()
        threading.Thread(target=pump_upstream_err, daemon=True).start()
        return True

    try:
        for line in sys.stdin:
            if upstream is None:
                method = ""
                try:
                    method = str(json.loads(line).get("method") or "")
                except (json.JSONDecodeError, ValueError):
                    pass
                home, label, reason = _pick_account()
                if not bind(home, label, reason):
                    if not bind(DEFAULT_HOME, "default", "spawn-retry-default"):
                        return 1
                _log(f"selected {bound_label} via {reason} for {method or 'unknown'}")
            assert upstream is not None and upstream.stdin is not None
            try:
                upstream.stdin.write(line)
                upstream.stdin.flush()
            except (OSError, ValueError):
                return 0
    finally:
        if upstream is not None:
            upstream.terminate()
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                upstream.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())