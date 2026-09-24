# hermes-antigravity-acp

Standalone integration for Google's official Antigravity ACP server. Keep vendor authentication inside the official ACP runtime; do not extract or reimplement OAuth tokens. Prefer standard ACP JSON-RPC over CLI/TUI scraping or proprietary stream-json bridges. Keep host-specific deployment glue optional and documented. Tests should exercise protocol behavior with fake ACP subprocesses and must not require live credentials.
