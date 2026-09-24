#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from antigravity_acp.registry import VERSION, resolve_distribution


def _find_executable(root: Path, name: str) -> Path:
    matches = [p for p in root.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {name!r} in archive, found {len(matches)}")
    return matches[0]


def _write_launcher(path: Path, target: Path, default_args: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        joined_args = " ".join(default_args)
        content = f'@echo off\r\n"{target}" {joined_args} %*\r\n'
    else:
        quoted_target = str(target).replace("'", "'\\''")
        quoted_args = " ".join("'" + arg.replace("'", "'\\''") + "'" for arg in default_args)
        suffix = f" {quoted_args}" if quoted_args else ""
        content = f"#!/bin/sh\nexec '{quoted_target}'{suffix} \"$@\"\n"
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Google's official Antigravity ACP server")
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path.home() / ".local" / "opt" / "antigravity-acp",
        help="Installation root (default: ~/.local/opt/antigravity-acp)",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="Launcher directory (default: ~/.local/bin)",
    )
    parser.add_argument(
        "--runtime-prefix",
        type=Path,
        default=None,
        help="Runtime-visible install root when staging through a host bind mount",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing pinned installation")
    args = parser.parse_args()

    dist = resolve_distribution()
    version_dir = args.prefix.expanduser().resolve() / VERSION
    launcher = args.bin_dir.expanduser().resolve() / (
        "antigravity-acp-official.cmd" if os.name == "nt" else "antigravity-acp-official"
    )

    def launch_target(exe: Path) -> Path:
        if args.runtime_prefix is None:
            return exe
        return args.runtime_prefix.expanduser() / VERSION / exe.relative_to(version_dir)

    if version_dir.exists():
        if not args.force:
            exe = _find_executable(version_dir, dist.executable)
            _write_launcher(launcher, launch_target(exe), dist.args)
            print(f"Already installed: {exe}")
            print(f"Launcher: {launcher}")
            return 0
        shutil.rmtree(version_dir)

    version_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="antigravity-acp-install-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "official.zip"
        print(f"Downloading official Antigravity ACP {VERSION} from Google...")
        urllib.request.urlretrieve(dist.url, archive)
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
        version_dir.mkdir(parents=True)
        for item in extract_dir.iterdir():
            destination = version_dir / item.name
            shutil.move(str(item), destination)

    exe = _find_executable(version_dir, dist.executable)
    if os.name != "nt":
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _write_launcher(launcher, launch_target(exe), dist.args)
    print(f"Installed: {exe}")
    print(f"Launcher: {launcher}")
    print("The installer did not authenticate or copy any credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
