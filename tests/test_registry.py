from antigravity_acp.registry import VERSION, resolve_distribution


def test_linux_x86_64_uses_google_registry_shape() -> None:
    dist = resolve_distribution("Linux", "x86_64")
    assert VERSION in dist.url
    assert dist.url.startswith("https://dl.google.com/agy-extensions/releases/linux/")
    assert dist.executable == "agy_acp_server.par"
    assert dist.args == ("--uid=",)


def test_arm64_aliases_by_platform() -> None:
    assert resolve_distribution("Linux", "arm64").url.endswith("linux-arm64.zip")
    assert resolve_distribution("Darwin", "arm64").url.endswith("darwin-arm64.zip")
