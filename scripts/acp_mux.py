#!/usr/bin/env python3
"""Antigravity ACP account multiplexer — one ACP endpoint, many Google accounts.

A client (for example Hermes' external_process provider) spawns this as one ACP
v1 stdio agent. Per client session the mux:

1. hot-loads an account registry,
2. chooses the healthiest account from the best priority tier, round-robin
   within that tier,
3. spawns Google's official ``agy_acp_server.par`` with that account's HOME,
4. relays ACP JSON-RPC unchanged while observing only explicit upstream
   failures for future routing decisions.

The mux deliberately does not open Antigravity credential files, exchange
refresh tokens, or call private/internal Google quota APIs. Authentication
remains inside Google's official ACP runtime.

Observed failures affect *future* sessions only. Quota/rate-limit, auth, and
unexpected process failures receive separate cooldowns. If every account is
cooling down, the account whose cooldown expires first is probed so the mux
never turns a stale health record into a hard outage.

Environment:
  ACP_MUX_HOME            shared mux control home; defaults to HERMES_REAL_HOME,
                          then HOME. Used only for default registry/state/log paths.
  ACP_MUX_ACCOUNTS        registry path (default ~/.hermes/acp-accounts.json)
  ACP_MUX_PAR             official agy_acp_server.par path (required)
  ACP_MUX_STATE           routing/health state (default ~/.hermes/cache/acp-mux-state.json)
  ACP_MUX_LOG             selection log (default ~/.hermes/logs/acp-mux.log)
  ACP_MUX_AUTH_COOLDOWN   auth failure cooldown seconds (default 300)
  ACP_MUX_QUOTA_COOLDOWN  quota/rate-limit cooldown seconds (default 900)
  ACP_MUX_CRASH_COOLDOWN  unexpected process failure cooldown seconds (default 60)

For deployment compatibility, HERMES_ACP_ACCOUNTS and
HERMES_ANTIGRAVITY_ACP_PAR are accepted as fallback names. ACP_MUX_CACHE is
accepted as a legacy alias for ACP_MUX_STATE.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Iterator

try:  # POSIX deployment gets cross-process state serialization; other OSes degrade safely.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


def _env(primary: str, fallback: str | None, default: str) -> str:
    value = os.environ.get(primary)
    if value is None and fallback:
        value = os.environ.get(fallback)
    return value if value is not None else default


def _control_home(env: dict[str, str] | None = None) -> str:
    """Shared mux control home, independent from an isolated subprocess HOME.

    Hermes intentionally rewrites ``HOME`` for profile-scoped subprocesses and
    publishes the OS-user home as ``HERMES_REAL_HOME``. The mux account
    registry and health state are host-level control-plane data, so their
    defaults should follow the real home while the selected ACP child still
    receives the account-specific HOME below.

    ``ACP_MUX_HOME`` is the explicit portable override for non-Hermes hosts.
    """
    values = os.environ if env is None else env
    raw = (
        values.get("ACP_MUX_HOME")
        or values.get("HERMES_REAL_HOME")
        or values.get("HOME")
        or os.path.expanduser("~")
    )
    return os.path.expanduser(raw)


def _default_control_path(relative: str, env: dict[str, str] | None = None) -> str:
    return os.path.join(_control_home(env), ".hermes", relative)


REGISTRY_PATH = os.path.expanduser(
    _env(
        "ACP_MUX_ACCOUNTS",
        "HERMES_ACP_ACCOUNTS",
        _default_control_path("acp-accounts.json"),
    )
)
STATE_PATH = os.path.expanduser(
    _env(
        "ACP_MUX_STATE",
        "ACP_MUX_CACHE",
        _default_control_path("cache/acp-mux-state.json"),
    )
)
LOG_PATH = os.path.expanduser(
    os.environ.get("ACP_MUX_LOG", _default_control_path("logs/acp-mux.log"))
)
PAR = _env("ACP_MUX_PAR", "HERMES_ANTIGRAVITY_ACP_PAR", "")
DEFAULT_HOME = _control_home()

AUTH_COOLDOWN = float(os.environ.get("ACP_MUX_AUTH_COOLDOWN", "300"))
QUOTA_COOLDOWN = float(os.environ.get("ACP_MUX_QUOTA_COOLDOWN", "900"))
CRASH_COOLDOWN = float(os.environ.get("ACP_MUX_CRASH_COOLDOWN", "60"))

_QUOTA_RE = re.compile(
    r"(?:\b429\b|resource[_ -]?exhausted|too many requests|rate[_ -]?limit(?:ed)?|"
    r"quota(?:\s+(?:exceeded|exhausted|depleted|reached|unavailable)))",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"(?:\b401\b|\b403\b|unauthenticated|invalid[_ -]?grant|"
    r"authentication\s+(?:failed|failure|required)|authorization\s+(?:failed|failure)|"
    r"(?:access|refresh|id)?\s*token\s+(?:expired|revoked|invalid))",
    re.IGNORECASE,
)


def _log(msg: str) -> None:
    try:
        directory = os.path.dirname(LOG_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _redact(line: str) -> str:
    line = re.sub(
        r"(access_token|refresh_token|id_token|code_verifier|client_secret)(=|:)\s*\S+",
        r"\1\2***",
        line,
        flags=re.I,
    )
    line = re.sub(r"ya29\.\S+", "ya29.***", line)
    return line[:500]


def _load_registry() -> list[dict]:
    """Return registry entries; malformed/missing registries fall back to HOME."""
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, list):
            return [a for a in accounts if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _account(entry: dict) -> tuple[str, str, int, str]:
    home = entry.get("home") or DEFAULT_HOME
    home = os.path.expanduser(str(home))
    label = str(entry.get("label") or os.path.basename(home.rstrip("/")) or "acp")
    priority = entry.get("priority")
    priority = priority if isinstance(priority, int) else 0
    key = f"{label}\t{home}"
    return home, label, priority, key


def _empty_state() -> dict:
    return {"version": 1, "cursor": {}, "accounts": {}}


def _load_state_unlocked() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _empty_state()
    except (OSError, json.JSONDecodeError):
        return _empty_state()

    cursor = data.get("cursor")
    accounts = data.get("accounts")
    return {
        "version": 1,
        "cursor": cursor if isinstance(cursor, dict) else {},
        "accounts": accounts if isinstance(accounts, dict) else {},
    }


def _save_state_unlocked(state: dict) -> None:
    try:
        directory = os.path.dirname(STATE_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = STATE_PATH + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


@contextmanager
def _state_lock() -> Iterator[None]:
    """Serialize cursor/health updates across concurrent mux processes on POSIX."""
    lock_path = STATE_PATH + ".lock"
    directory = os.path.dirname(lock_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        lock = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        yield
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _cooldown_for(kind: str) -> float:
    if kind == "auth":
        return AUTH_COOLDOWN
    if kind == "quota":
        return QUOTA_COOLDOWN
    return CRASH_COOLDOWN


def _mark_failure(home: str, label: str, kind: str, detail: str = "") -> None:
    """Persist one observed failure without inspecting vendor credentials."""
    now = time.time()
    key = f"{label}\t{home}"
    with _state_lock():
        state = _load_state_unlocked()
        accounts = state["accounts"]
        current = accounts.get(key)
        current = current if isinstance(current, dict) else {}

        # One upstream failure may be repeated on stdout/stderr. Do not keep
        # extending the same cooldown for duplicate lines from the same event.
        last_at = current.get("last_failure_at", 0)
        if current.get("reason") == kind and isinstance(last_at, (int, float)) and now - last_at < 5:
            return

        cooldown_until = max(
            float(current.get("cooldown_until", 0) or 0),
            now + _cooldown_for(kind),
        )
        accounts[key] = {
            "label": label,
            "home": home,
            "reason": kind,
            "cooldown_until": cooldown_until,
            "last_failure_at": now,
            "failures": int(current.get("failures", 0) or 0) + 1,
            "detail": _redact(detail),
        }
        _save_state_unlocked(state)
    _log(f"quarantine {label}: {kind} until={int(cooldown_until)}")


def _pick_account(now: float | None = None) -> tuple[str, str, str]:
    """Pick the best healthy priority tier, round-robin within that tier."""
    entries = _load_registry()
    if not entries:
        return DEFAULT_HOME, "default", "no-registry"

    now = time.time() if now is None else now
    rows = []
    for index, entry in enumerate(entries):
        home, label, priority, key = _account(entry)
        rows.append((priority, index, home, label, key))

    with _state_lock():
        state = _load_state_unlocked()
        health = state["accounts"]
        cursor = state["cursor"]

        for priority in sorted({row[0] for row in rows}):
            pool = []
            for row in rows:
                if row[0] != priority:
                    continue
                record = health.get(row[4])
                record = record if isinstance(record, dict) else {}
                until = record.get("cooldown_until", 0)
                until = float(until) if isinstance(until, (int, float)) else 0.0
                if until <= now:
                    pool.append(row)
            if not pool:
                continue

            cursor_key = str(priority)
            raw_cursor = cursor.get(cursor_key, 0)
            position = int(raw_cursor) if isinstance(raw_cursor, int) else 0
            chosen = pool[position % len(pool)]
            cursor[cursor_key] = (position + 1) % len(pool)
            _save_state_unlocked(state)
            reason = "round-robin" if len(pool) > 1 else "priority"
            return chosen[2], chosen[3], reason

        # Every account is cooling down. Probe the one expected to recover
        # first instead of refusing service based on stale observations.
        candidates = []
        for row in rows:
            record = health.get(row[4])
            record = record if isinstance(record, dict) else {}
            until = record.get("cooldown_until", 0)
            until = float(until) if isinstance(until, (int, float)) else 0.0
            candidates.append((until, row[0], row[1], row))
        _, _, _, chosen = min(candidates)
        return chosen[2], chosen[3], "cooldown-probe"


def _classify_failure(text: str) -> str | None:
    if _QUOTA_RE.search(text):
        return "quota"
    if _AUTH_RE.search(text):
        return "auth"
    return None


def _classify_json_error(line: str) -> tuple[str, str] | None:
    """Classify only structured ACP/JSON-RPC errors, never normal model content."""
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or "error" not in payload:
        return None
    detail = json.dumps(payload["error"], ensure_ascii=False)
    kind = _classify_failure(detail)
    return (kind, detail) if kind else None


def main() -> int:
    if not PAR or not os.path.exists(PAR):
        sys.stderr.write("antigravity-acp-mux: set ACP_MUX_PAR to the official agy_acp_server.par\n")
        return 2

    # Buffer stdin until the first request (normally initialize), then bind one
    # official ACP child for the lifetime of this client session.
    upstream: subprocess.Popen[str] | None = None
    bound_home = ""
    bound_label = ""
    pump_threads: list[threading.Thread] = []

    def pump_upstream_out() -> None:
        assert upstream is not None
        for line in upstream.stdout or ():
            failure = _classify_json_error(line)
            if failure:
                _mark_failure(bound_home, bound_label, failure[0], failure[1])
            sys.stdout.write(line)
            sys.stdout.flush()

    def pump_upstream_err() -> None:
        assert upstream is not None
        for line in upstream.stderr or ():
            redacted = _redact(line.rstrip("\n"))
            kind = _classify_failure(redacted)
            if kind:
                _mark_failure(bound_home, bound_label, kind, redacted)
            sys.stderr.write(redacted + "\n")
            sys.stderr.flush()

    def bind(home: str, label: str, reason: str) -> bool:
        nonlocal upstream, bound_home, bound_label
        env = dict(os.environ)
        env["HOME"] = home
        try:
            upstream = subprocess.Popen(
                [PAR, "--uid="],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            _log(f"spawn-failed {label}: {exc}")
            return False
        bound_home = home
        bound_label = label
        _log(f"bound account {label} ({reason}) pid={upstream.pid}")
        out_thread = threading.Thread(target=pump_upstream_out, daemon=True)
        err_thread = threading.Thread(target=pump_upstream_err, daemon=True)
        pump_threads.extend((out_thread, err_thread))
        out_thread.start()
        err_thread.start()
        return True

    try:
        for line in sys.stdin:
            if upstream is None:
                method = ""
                try:
                    payload = json.loads(line)
                    method = str(payload.get("method") or "") if isinstance(payload, dict) else ""
                except (json.JSONDecodeError, ValueError):
                    pass
                home, label, reason = _pick_account()
                if not bind(home, label, reason):
                    if not bind(DEFAULT_HOME, "default", "spawn-retry-default"):
                        return 1
                _log(f"selected {bound_label} via {reason} for {method or 'unknown'}")

            assert upstream is not None and upstream.stdin is not None
            if upstream.poll() not in (None, 0):
                _mark_failure(bound_home, bound_label, "crash", f"exit={upstream.returncode}")
                return 0
            try:
                upstream.stdin.write(line)
                upstream.stdin.flush()
            except (OSError, ValueError):
                returncode = upstream.poll()
                if returncode not in (None, 0):
                    _mark_failure(bound_home, bound_label, "crash", f"exit={returncode}")
                return 0
    finally:
        if upstream is not None:
            if upstream.stdin is not None and not upstream.stdin.closed:
                try:
                    upstream.stdin.close()
                except OSError:
                    pass
            if upstream.poll() is None:
                try:
                    # Give a well-behaved ACP child a chance to flush its final
                    # JSON-RPC response and exit after stdin EOF.
                    upstream.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    upstream.terminate()
                    try:
                        upstream.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        upstream.kill()
                        upstream.wait(timeout=5)
            for thread in pump_threads:
                thread.join(timeout=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
