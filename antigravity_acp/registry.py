from __future__ import annotations

import platform
from dataclasses import dataclass

VERSION = "1.1.1"


@dataclass(frozen=True)
class Distribution:
    url: str
    executable: str
    args: tuple[str, ...] = ()


_DISTRIBUTIONS: dict[tuple[str, str], Distribution] = {
    ("linux", "x86_64"): Distribution(
        url=(
            "https://dl.google.com/agy-extensions/releases/linux/"
            "agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip"
        ),
        executable="agy_acp_server.par",
        args=("--uid=",),
    ),
    ("linux", "aarch64"): Distribution(
        url=(
            "https://dl.google.com/agy-extensions/releases/linux/"
            "agy-acp-server-agy_acp_server_1.1.1-linux-arm64.zip"
        ),
        executable="agy_acp_server.par",
        args=("--uid=",),
    ),
    ("darwin", "arm64"): Distribution(
        url=(
            "https://dl.google.com/agy-extensions/releases/macos/"
            "agy-acp-server-agy_acp_server_1.1.1-darwin-arm64.zip"
        ),
        executable="agy_acp_server.par",
    ),
    ("windows", "amd64"): Distribution(
        url=(
            "https://dl.google.com/agy-extensions/releases/windows/"
            "agy-acp-server-agy_acp_server_1.1.1-windows-x86_64.zip"
        ),
        executable="agy_acp_server.exe",
    ),
    ("windows", "arm64"): Distribution(
        url=(
            "https://dl.google.com/agy-extensions/releases/windows/"
            "agy-acp-server-agy_acp_server_1.1.1-windows-arm64.zip"
        ),
        executable="agy_acp_server.exe",
    ),
}


def resolve_distribution(system: str | None = None, machine: str | None = None) -> Distribution:
    system = (system or platform.system()).strip().lower()
    machine = (machine or platform.machine()).strip().lower()
    aliases = {
        "x86-64": "x86_64",
        "x64": "x86_64",
        "amd64": "amd64" if system == "windows" else "x86_64",
        "arm64": "arm64" if system in {"darwin", "windows"} else "aarch64",
    }
    machine = aliases.get(machine, machine)
    try:
        return _DISTRIBUTIONS[(system, machine)]
    except KeyError as exc:
        supported = ", ".join(f"{s}/{m}" for s, m in sorted(_DISTRIBUTIONS))
        raise RuntimeError(f"Unsupported platform {system}/{machine}. Supported: {supported}") from exc
