"""Hermes provider plugin for Google's official Antigravity ACP server."""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class AntigravityACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> Any:
        from agent.copilot_acp_client import CopilotACPClient

        client_kwargs.setdefault("command", self.process_command)
        client_kwargs.setdefault("args", list(self.process_args))
        client_kwargs.setdefault("base_url", self.base_url)
        return CopilotACPClient(**client_kwargs)

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> list[str] | None:
        try:
            client = self.create_client(
                api_key=api_key,
                base_url=base_url or self.base_url,
                command=self.process_command,
                args=list(self.process_args),
            )
            return client.list_models(timeout_seconds=timeout) or None
        except Exception:
            return None


antigravity_acp = AntigravityACPProfile(
    name="antigravity-acp",
    aliases=("agy-acp", "antigravity-official-acp"),
    display_name="Antigravity ACP (official)",
    description="Google Antigravity through Google's official ACP v1 stdio server",
    api_mode="chat_completions",
    env_vars=(),
    base_url="acp://antigravity",
    auth_type="external_process",
    process_command="antigravity-acp-official",
    # MUST be non-empty: CopilotACPClient treats falsy args as "use the default
    # --acp --stdio", which the official par rejects (FATAL Flags parsing error:
    # Unknown command line flag 'acp'). fetch_models() swallows that failure and
    # the picker silently shows only fallback_models instead of the live
    # configOptions catalog. "--uid=" is the arg the official wrapper itself uses.
    process_args=("--uid=",),
    process_command_env_vars=("HERMES_ANTIGRAVITY_ACP_COMMAND",),
    process_args_env_var="HERMES_ANTIGRAVITY_ACP_ARGS",
    # No stub fallback: "antigravity-acp" is a transport label, not a model.
    # When the live ACP session cannot list models, show nothing rather than a
    # phantom entry that fails on first use.
    fallback_models=(),
)

register_provider(antigravity_acp)
