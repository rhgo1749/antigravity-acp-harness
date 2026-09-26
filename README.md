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
2. queries each account's live quota via the read-only `retrieveUserQuotaSummary` endpoint (results cached ~60s),
3. binds the session to the account with the best remaining quota (minimum `remainingFraction` across all windows; ties broken by registry priority), spawning the official `agy_acp_server.par` under that account's `HOME` and relaying JSON-RPC both ways.

```bash
cp examples/acp-accounts.example.json ~/.hermes/acp-accounts.json   # edit accounts
ACP_MUX_PAR=~/.local/opt/antigravity-acp/<version>/agy_acp_server.par \
  python3 scripts/acp_mux.py --uid=
```

Accounts are isolated purely by `HOME`: each account's own
`$HOME/.gemini/antigravity-acp/acp_token.json` is created by the **official
server's own OAuth flow** (spawn it with `HOME=<account home>`, call ACP
`authenticate` with `oauth-personal`, complete the consent in a browser). The
mux never writes, copies, or mints OAuth tokens; it reads the credential the
official server already owns solely to call the read-only quota endpoint.
Tokens stay in process memory: the relay stderr is redacted and the selection
log records account labels and scores only.

Point a Hermes provider profile's `process_command` at a small wrapper that
exports `ACP_MUX_PAR` and execs `python3 scripts/acp_mux.py` to use it as a
model backend.

Degradation order: quota-scoreable accounts > credential-usable accounts by
priority > default `HOME`. Accounts failing auth get a 5-minute penalty so a
revoked account does not slow every spawn. Account selection happens at
session start; in-session failover on a mid-session 429 is not implemented.

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
