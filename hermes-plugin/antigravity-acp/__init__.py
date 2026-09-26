"""Hermes provider plugin for Google's official Antigravity ACP server."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_RATE_LIMIT_RE = re.compile(
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
_DEFAULT_RATE_LIMIT_RETRIES = 2
_MAX_RATE_LIMIT_RETRIES = 8


def _rate_limit_retries() -> int:
    raw = os.environ.get(
        "HERMES_ANTIGRAVITY_ACP_RATE_LIMIT_RETRIES",
        str(_DEFAULT_RATE_LIMIT_RETRIES),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_RATE_LIMIT_RETRIES
    return max(0, min(value, _MAX_RATE_LIMIT_RETRIES))


def _is_failover_error(exc: BaseException) -> bool:
    return bool(_RATE_LIMIT_RE.search(str(exc)) or _AUTH_RE.search(str(exc)))


class AntigravityACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> Any:
        from agent.copilot_acp_client import CopilotACPClient

        class RetryingAntigravityACPClient(CopilotACPClient):
            def _run_prompt(
                self,
                prompt_text: str,
                *,
                timeout_seconds: float,
                model: str | None = None,
            ) -> tuple[str, str]:
                retries = _rate_limit_retries()
                deadline = time.monotonic() + timeout_seconds
                last_exc: RuntimeError | None = None

                for attempt in range(retries + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if last_exc is not None:
                            raise last_exc
                        raise TimeoutError("Timed out before Antigravity ACP prompt could start.")
                    try:
                        return super()._run_prompt(
                            prompt_text,
                            timeout_seconds=remaining,
                            model=model,
                        )
                    except RuntimeError as exc:
                        if attempt >= retries or not _is_failover_error(exc):
                            raise
                        last_exc = exc
                        logger.warning(
                            "Antigravity ACP failover attempt %d/%d; retrying with a fresh ACP session.",
                            attempt + 1,
                            retries + 1,
                        )

                assert last_exc is not None
                raise last_exc

        client_kwargs.setdefault("command", self.process_command)
        client_kwargs.setdefault("args", list(self.process_args))
        client_kwargs.setdefault("base_url", self.base_url)
        return RetryingAntigravityACPClient(**client_kwargs)

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
