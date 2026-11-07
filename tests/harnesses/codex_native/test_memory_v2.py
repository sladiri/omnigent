"""Shared-state memory v2 experiment; no real Codex data or model calls."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.harnesses.codex_native.bridge import _codex_orphan_retention_expired
from omnigent.harnesses.codex_native.memory import (
    SHARED_MEMORY_MARKER,
    prepare_shared_memory,
    shared_memory_command,
)
from omnigent.inner.codex_executor import _populate_codex_home_config
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _set_codex_version,
    _test_app_server,
)


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    source, home = tmp_path / "source", tmp_path / "session"
    source.mkdir()
    home.mkdir()
    (source / "config.toml").write_text(
        'model = "source-model"\n[features]\nmemories = true\n[memories]\nversion = "v2"\n'
    )
    _populate_codex_home_config(home, source, minimal_config=False)
    return source, home


def test_two_sessions_share_artifacts_database_and_workspace_lock(homes, tmp_path: Path) -> None:
    source, first = homes
    second = tmp_path / "second"
    second.mkdir()
    for home in (first, second):
        prepare_shared_memory(home, source)
        config = tomlkit.parse((home / "config.toml").read_text())
        assert config["sqlite_home"] == str(source)
        assert (home / "memories_v2").is_dir()
        assert not (home / "memories_v2").is_symlink()
    script = (
        "from pathlib import Path; p=Path('memories_v2/memory_summary.md'); "
        "p.write_text('Useful verified fact')"
    )
    subprocess.run(
        shared_memory_command(first, [sys.executable, "-c", script]), cwd=first, check=True
    )
    script = (
        "from pathlib import Path; "
        "assert Path('memories_v2/memory_summary.md').read_text() == 'Useful verified fact'"
    )
    subprocess.run(
        shared_memory_command(second, [sys.executable, "-c", script]), cwd=second, check=True
    )
    assert not (first / "memories_v2" / "memory_summary.md").exists()
    assert (source / "memories_v2" / "memory_summary.md").exists()
    with (first / "memories_v2.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (second / "memories_v2.lock").open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inode = os.fstat(owner.fileno()).st_ino
        prepare_shared_memory(second, source)
        assert (second / "memories_v2.lock").stat().st_ino == inode


def test_existing_session_refreshes_only_memory_settings_and_preserves_database(homes) -> None:
    source, home = homes
    prepare_shared_memory(home, source)
    config = tomlkit.parse((home / "config.toml").read_text())
    config["model"] = "private-model"
    config["developer_instructions"] = "private instruction"
    (home / "config.toml").write_text(tomlkit.dumps(config))
    (source / "config.toml").write_text('model = "new-source"\n[features]\nmemories = false\n')
    prepare_shared_memory(home, source)
    updated = tomlkit.parse((home / "config.toml").read_text())
    assert updated["features"]["memories"] is False
    assert updated["model"] == "private-model"
    assert updated["developer_instructions"] == "private instruction"
    assert updated["sqlite_home"] == str(source)
    assert (home / "memories_v2").is_dir()
    assert "private-model" not in (source / "config.toml").read_text()


@pytest.mark.parametrize("configured", ["./database", "absolute"])
def test_respects_configured_sqlite_home(homes, tmp_path: Path, configured: str) -> None:
    source, home = homes
    value = str(tmp_path / "database") if configured == "absolute" else configured
    config_path = source / "config.toml"
    config_path.write_text(f"sqlite_home = {json.dumps(value)}\n" + config_path.read_text())
    prepare_shared_memory(home, source)
    expected = tmp_path / "database" if configured == "absolute" else source / "database"
    assert tomlkit.parse((home / "config.toml").read_text())["sqlite_home"] == str(expected)


def test_profile_memory_settings_override_base(homes) -> None:
    source, home = homes
    (source / "config.toml").write_text("[features]\nmemories = false\n")
    (source / "work.config.toml").write_text(
        '[features]\nmemories = true\n[memories]\nversion = "v2"\nuse_memories = false\n'
    )
    prepare_shared_memory(home, source, profile="work")
    config = tomlkit.parse((home / "config.toml").read_text())
    assert config["features"]["memories"] is True
    assert config["memories"]["use_memories"] is False


@pytest.mark.parametrize("name", ["memories_v2", "memories_v2.lock", "state_5.sqlite"])
def test_existing_private_data_is_refused_and_preserved(homes, name: str) -> None:
    source, home = homes
    existing = home / name
    existing.write_text("preserve me")
    before = (home / "config.toml").read_bytes()
    with pytest.raises(ValueError, match=r"existing|independent"):
        prepare_shared_memory(home, source)
    assert existing.read_text() == "preserve me"
    assert (home / "config.toml").read_bytes() == before
    assert not (home / SHARED_MEMORY_MARKER).exists()


def test_changed_storage_is_refused(homes) -> None:
    source, home = homes
    prepare_shared_memory(home, source)
    source_config = source / "config.toml"
    source_config.write_text('sqlite_home = "different"\n' + source_config.read_text())
    with pytest.raises(ValueError, match="storage changed"):
        prepare_shared_memory(home, source)
    assert tomlkit.parse((home / "config.toml").read_text())["sqlite_home"] == str(source)


def test_disabled_and_v1_profiles_do_not_share_state(homes) -> None:
    source, home = homes
    for config in (
        "[features]\nmemories = false\n",
        '[features]\nmemories = true\n[memories]\nversion = "v1"\n',
    ):
        (source / "config.toml").write_text(config)
        prepare_shared_memory(home, source)
        assert not (home / SHARED_MEMORY_MARKER).exists()
        assert not (source / "memories_v2").exists()


def test_shared_rollout_home_survives_orphan_collection(homes, tmp_path: Path) -> None:
    source, _ = homes
    bridge = tmp_path / "bridge"
    home = bridge / "codex-home"
    home.mkdir(parents=True)
    marker = bridge / "owner.pid"
    marker.write_text("999999")
    os.utime(marker, (0, 0))
    assert _codex_orphan_retention_expired(bridge)
    prepare_shared_memory(home, source)
    assert not _codex_orphan_retention_expired(bridge)


@pytest.mark.parametrize("enabled", [True, False])
async def test_native_start_pins_memory_config_uses_mount_and_stops_child(
    homes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    source, home = homes
    monkeypatch.setenv("CODEX_HOME", str(source))
    if not enabled:
        prepare_shared_memory(home, source)
        (source / "config.toml").write_text("[features]\nmemories = false\n")
    _set_codex_version(monkeypatch, (0, 165, 0))
    _disable_codex_startup_rpc(monkeypatch)
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, signal\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['CODEX_HOME']) / 'memories_v2'\n"
        "(root / 'child.pid').write_text(str(os.getpid()))\n"
        "signal.pause()\n"
    )
    executable.chmod(0o700)
    server: CodexNativeAppServer = _test_app_server(tmp_path, home, tmp_path / "bridge", tmp_path)
    server.codex_path = str(executable)
    server.config_overrides = [
        'sqlite_home="wrong-database"',
        f"features.memories={str(not enabled).lower()}",
        'memories.version="v1"',
    ]
    child_record = source / "memories_v2" / "child.pid"
    await server.start()
    try:
        async with asyncio.timeout(5):
            while not child_record.exists():
                await asyncio.sleep(0.01)
        child_pid = int(child_record.read_text())
        assert server.proc is not None
        assert server.process_registry_tag is not None
        cmdline = Path(f"/proc/{server.proc.pid}/cmdline").read_bytes()
        assert server.process_registry_tag.encode() in cmdline
        assert cmdline.decode().split("\0")[-7:-1] == [
            "-c",
            f"sqlite_home={json.dumps(str(source))}",
            "-c",
            f"features.memories={str(enabled).lower()}",
            "-c",
            'memories.version="v2"',
        ]
    finally:
        await server.close()
    assert server.proc is None
    child_status = Path(f"/proc/{child_pid}/stat")
    assert not child_status.exists() or child_status.read_text().split()[2] == "Z"
