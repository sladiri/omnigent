"""Experimental shared memory v2 state for persistent native Codex sessions."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import tomlkit

from omnigent.harnesses.codex_native.launch_args import (
    _merge_tables,
    _write_private_config,
    absolute_codex_path,
    codex_config_profile,
)

SHARED_MEMORY_MARKER = ".omnigent-shared-memory.json"


def prepare_shared_memory(home: Path, source_home: Path, *, profile: str | None = None) -> None:
    """Share v2 storage for opted-in native sessions without replacing their config.

    Existing independent databases require a separate migration. Retain the
    chosen shared database after disabling memories so resume still works.
    """
    source_home = source_home.resolve()
    if home.resolve() == source_home:
        raise ValueError("Shared Codex memory requires a private session home")
    source_path = source_home / "config.toml"
    source = tomlkit.parse(source_path.read_text()).unwrap() if source_path.exists() else {}
    if profile is not None:
        codex_config_profile(["--profile", profile])
        overlay = tomlkit.parse((source_home / f"{profile}.config.toml").read_text()).unwrap()
        _merge_tables(source, overlay)
    enabled = source.get("features", {}).get("memories") is True
    memories = source.get("memories", {})
    selected = enabled and memories.get("version") == "v2"
    marker = home / SHARED_MEMORY_MARKER
    if not selected and not marker.exists():
        return
    if enabled and not selected:
        raise ValueError("Disable memories before changing a shared memory v2 session's version")
    sqlite_home = Path(
        absolute_codex_path(
            source.get("sqlite_home") or os.environ.get("CODEX_SQLITE_HOME") or str(source_home),
            source_home,
        )
    ).resolve()
    selection = {"source_home": str(source_home), "sqlite_home": str(sqlite_home)}
    if marker.exists():
        previous = json.loads(marker.read_text())
        if previous != selection:
            raise ValueError(
                "Shared Codex memory storage changed; migrate the session before restarting"
            )
    elif any(home.glob("state_*.sqlite")):
        raise ValueError(
            "This Codex session has an independent database. Start a new Omnigent session "
            "for shared memory v2; existing history needs a separate migration."
        )

    memory_root = home / "memories_v2"
    if memory_root.is_symlink() or (
        memory_root.exists() and (not memory_root.is_dir() or any(memory_root.iterdir()))
    ):
        raise ValueError(f"Refusing to replace existing Codex memory path: {memory_root}")
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        raise ValueError("Shared memory v2 experiment requires Linux and bubblewrap")
    links = {home / "memories_v2.lock": source_home / "memories_v2.lock"}
    for link, destination in links.items():
        if (link.exists() or link.is_symlink()) and (
            not link.is_symlink() or link.resolve() != destination.resolve()
        ):
            raise ValueError(f"Refusing to replace existing Codex memory path: {link}")
    sqlite_home.mkdir(parents=True, exist_ok=True)
    (source_home / "memories_v2").mkdir(exist_ok=True)
    memory_root.mkdir(exist_ok=True)
    # Keep the lock inode stable across sessions and Codex workspace resets.
    with (source_home / "memories_v2.lock").open("a"):
        pass
    for link, destination in links.items():
        if not link.is_symlink():
            link.symlink_to(destination)

    config_path = home / "config.toml"
    config = tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    config.setdefault("features", {})["memories"] = selected
    config["memories"] = memories
    config["sqlite_home"] = str(sqlite_home)
    _write_private_config(config_path, tomlkit.dumps(config))
    _write_private_config(marker, json.dumps(selection) + "\n")


def shared_memory_config_overrides(home: Path, overrides: Sequence[str]) -> list[str]:
    """Pin storage and feature selection after caller-supplied CLI overrides."""
    marker = home / SHARED_MEMORY_MARKER
    if not marker.exists():
        return list(overrides)
    selection = json.loads(marker.read_text())
    config = tomlkit.parse((home / "config.toml").read_text())
    enabled = config["features"]["memories"]
    return [
        *overrides,
        f"sqlite_home={json.dumps(selection['sqlite_home'])}",
        f"features.memories={str(enabled).lower()}",
        'memories.version="v2"',
    ]


def shared_memory_command(
    home: Path, command: list[str], *, argv0: str | None = None
) -> list[str]:
    """Bind shared artifacts only inside the child process's mount namespace."""
    marker = home / SHARED_MEMORY_MARKER
    if not marker.exists():
        return command
    source_home = Path(json.loads(marker.read_text())["source_home"])
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValueError("Shared memory v2 experiment requires bubblewrap")
    return [
        bwrap,
        "--die-with-parent",
        "--dev-bind",
        "/",
        "/",
        "--bind",
        str(source_home / "memories_v2"),
        str(home / "memories_v2"),
        "--argv0",
        argv0 or command[0],
        "--",
        *command,
    ]
