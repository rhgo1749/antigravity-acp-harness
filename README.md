# antigravity-acp-harness

Use Google's **official Antigravity ACP server** as a standard ACP v1 agent backend, without scraping the Antigravity TUI or reimplementing its private transport.

This repository does **not** redistribute Google's proprietary binaries and does **not** extract, copy, or emulate Google OAuth tokens. The installer downloads the official `antigravity-acp` distribution directly from Google's CDN using the version and platform mapping published in the Agent Client Protocol Registry.

## Why this exists

Google now publishes a standalone Antigravity ACP server (`agy_acp_server`) that speaks newline-delimited JSON-RPC over stdio. That means an ACP client can integrate the Antigravity agent runtime through a structured protocol instead of a PTY, TUI parser, or a custom `agy --input-format stream-json` bridge.

```text
ACP client / harness
        |
        | ACP v1 (JSON-RPC over stdio)
        v
Google agy_acp_server
        |
        v
Antigravity agent runtime
```

The official server currently advertises authentication methods including `oauth-personal`, `oauth-business`, `gemini-api-key`, and `agent-platform`.

## Important: personal Antigravity OAuth and third-party harnesses

Google's current Antigravity Terms and FAQ explicitly say that using an **individual Antigravity login/OAuth through third-party software** (their examples include OpenClaw and OpenCode) violates the Antigravity terms and may lead to suspension or termination.

Accordingly, this project does **not** present personal Google AI / Antigravity subscription quota as a third-party API substitute and does not automate personal OAuth for third-party harnesses. For third-party clients, use an authentication route permitted by the terms that apply to you, such as a Gemini API key / Google AI Studio or an applicable Gemini Enterprise / organizational route, unless Google has explicitly authorized another path for your account and client.

Official references:

- Google Antigravity Terms: https://antigravity.google/terms
- Google Antigravity FAQ: https://www.antigravity.google/docs/faq/
- ACP Registry entry: https://github.com/agentclientprotocol/registry/tree/main/registry/antigravity-acp

## Install the official server

Linux x86_64 example:

```bash
python3 scripts/install_official.py
~/.local/bin/antigravity-acp-official --help
```

The installer pins the Registry version declared in `antigravity_acp/registry.py`, downloads directly from `dl.google.com`, extracts into `~/.local/opt/antigravity-acp/<version>/`, and creates a launcher in `~/.local/bin/`.

Override the destination without touching your home directory:

```bash
python3 scripts/install_official.py --prefix /tmp/agy-acp
```

If you stage the install through a host bind mount but the launcher will run under a different path inside a container, set the runtime-visible root separately:

```bash
python3 scripts/install_official.py \
  --prefix /host/bundle/container-home/.local/opt/antigravity-acp \
  --bin-dir /host/bundle/container-home/.local/bin \
  --runtime-prefix /home/hermes/.local/opt/antigravity-acp
```

## Verify ACP, without authenticating

```bash
python3 scripts/acp_probe.py -- antigravity-acp-official
```

On Linux the launcher automatically supplies Google's Registry argument `--uid=`. A successful probe prints the negotiated protocol version, agent name/version, and advertised auth methods. It does not send a prompt or perform login.

## Use with an ACP client

The important contract is simply a child process whose stdin/stdout carry ACP v1 JSON-RPC. A generic client configuration is:

```json
{
  "command": "antigravity-acp-official",
  "args": []
}
```

Clients differ in where they store that configuration. Zed and JetBrains can consume external ACP agents directly. OpenCode and Pi can themselves be exposed *as* ACP agents; using Antigravity *inside* those harnesses requires a client-side ACP integration/plugin in that harness rather than merely enabling `opencode acp` or `pi-acp`.

## Hermes provider plugin

`hermes-plugin/antigravity-acp/` is a standalone Hermes model-provider plugin. It replaces the old local `agy stream-json` transport with the official ACP wire while preserving Hermes as the owner of its own tool loop.

Install it into a Hermes profile:

```bash
mkdir -p ~/.hermes/plugins/model-providers
cp -a hermes-plugin/antigravity-acp ~/.hermes/plugins/model-providers/
```

The plugin expects `antigravity-acp-official` on `PATH`, or set the command through the provider configuration supported by your Hermes build. It starts the official server, sends `initialize` / `session/new` / `session/set_config_option` / `session/prompt`, consumes `session/update`, and maps the response back into Hermes' provider client shape.

This plugin deliberately keeps **Hermes tools in Hermes**. It does not grant the Antigravity runtime arbitrary native tool permissions when acting as a Hermes model backend. If you want the full Antigravity agent runtime with its native tools and MCP clients, connect the official server as an external ACP **agent**, not as a Hermes model-provider shim.

## Multi-account multiplexer (optional)

`scripts/acp_mux.py` presents **one** ACP endpoint backed by **several** Google accounts. A client spawns the mux instead of the official launcher; per spawn (i.e. per client session) the mux:

1. reads an account registry (JSON, hot-reloaded — add/remove accounts by editing the file only),
2. chooses the first priority tier that has a healthy account and round-robins within that tier,
3. spawns the official `agy_acp_server.par` under the selected account's `HOME` and relays ACP JSON-RPC unchanged,
4. observes explicit auth, quota/rate-limit, and child-process failures and quarantines that account for future sessions.

The mux does **not** read Antigravity credential files, exchange refresh tokens, or call Google private/internal quota APIs. Vendor authentication stays inside Google's official ACP process.

```bash
cp examples/acp-accounts.example.json ~/.hermes/acp-accounts.json   # edit accounts
ACP_MUX_PAR=~/.local/opt/antigravity-acp/<version>/agy_acp_server.par \
  python3 scripts/acp_mux.py --uid=
```

Accounts are isolated purely by `HOME`. Authentication and credential storage are owned by the **official** server running under each account HOME; the mux never opens those credentials. Relay stderr is redacted, and the routing state records only account labels/HOMEs, cooldown timestamps, failure classes, and a round-robin cursor.

When the parent harness isolates subprocess `HOME` per profile, the mux keeps its control-plane paths separate from that isolation. Default registry/state/log paths resolve from `ACP_MUX_HOME`, then `HERMES_REAL_HOME`, then `HOME`. Hermes already exports `HERMES_REAL_HOME` for profile-scoped subprocesses, so all profiles can share one account registry without machine-specific hardcoded paths. `ACP_MUX_ACCOUNTS`, `ACP_MUX_STATE`, and `ACP_MUX_LOG` remain the highest-priority per-path overrides.

Point a Hermes provider profile's `process_command` at a small wrapper that
exports `ACP_MUX_PAR` and execs `python3 scripts/acp_mux.py` to use it as a
model backend.

Accounts with the same numeric `priority` form a round-robin pool. Lower numbers are preferred tiers, so set all accounts to the same priority when you want even distribution; use different priorities when you want primary/fallback behavior.

Observed failures affect future sessions: auth failures default to a 5-minute cooldown, quota/rate-limit failures to 15 minutes, and unexpected child-process failures to 1 minute. These durations are configurable with `ACP_MUX_AUTH_COOLDOWN`, `ACP_MUX_QUOTA_COOLDOWN`, and `ACP_MUX_CRASH_COOLDOWN`. If every account is cooling down, the mux probes the account whose cooldown expires first rather than creating a hard outage from stale health state.

The bundled Hermes provider adds bounded **same-request failover** for rate-limit/quota errors. Hermes already creates a fresh ACP process/session for each completion and resends the full conversation transcript, so when account A returns `429` / `RESOURCE_EXHAUSTED`, the mux quarantines A and the provider retries that same completion through a fresh mux process. With the default `HERMES_ANTIGRAVITY_ACP_RATE_LIMIT_RETRIES=2`, up to three accounts can be tried within the original completion timeout budget. Set the environment variable to `0` to disable retry or to another bounded value (maximum 8) for a larger pool.

Note: when accounts authenticate with personal Antigravity OAuth, the terms
caveat above applies to the multiplexer exactly as it does to any third-party
harness — see "Important: personal Antigravity OAuth and third-party
harnesses".

## What this repository is not

- Not an OAuth extractor.
- Not a proxy that turns a consumer subscription into an OpenAI-compatible HTTP API.
- Not a replacement implementation of Antigravity.
- Not a redistribution of Google's proprietary ACP binary.
- Not a claim that every authentication method advertised by the official binary is permitted in every third-party client.

## License

Code in this repository is MIT licensed. Google's Antigravity binaries and services remain governed by Google's licenses and terms; see `NOTICE.md`.
