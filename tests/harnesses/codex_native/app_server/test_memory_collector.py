"""The collector reader opt-in composes safely with normal Omnigent startup."""

import sys

import tomllib

from omnigent.harnesses.codex_native.app_server import build_codex_native_server
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _set_codex_version,
)


async def test_reader_start_preserves_rules_and_overrides_shared_sqlite(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    original = (
        'developer_instructions = "USER RULE"\nsqlite_home = "/shared"\n'
        "[features]\nmemories = true\n"
    )
    (source / "config.toml").write_text(original)
    monkeypatch.setenv("CODEX_HOME", str(source))
    collector = tmp_path / "collector"
    memories = collector / "memories_v2"
    memories.mkdir(parents=True)
    (memories / "memory_summary.md").write_text("PROJECT LESSON")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _disable_codex_startup_rpc(monkeypatch)
    _set_codex_version(monkeypatch, (0, 165, 0))
    monkeypatch.setenv("OMNIGENT_CODEX_MEMORY_COLLECTOR_HOME", str(collector))
    server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "reader",
        bridge_dir=tmp_path / "bridge",
        cwd=workspace,
        model=None,
        profile=None,
        codex_path=sys.executable,
        developer_instructions="AGENT RULE",
        extra_config_overrides=["features.memories=true", 'sqlite_home="/shared"'],
        reconcile_process_registry=False,
    )
    assert server.env["OMNIGENT_CODEX_MEMORY_COLLECTOR_HOME"] == str(collector)
    await server.start()
    try:
        result = tomllib.loads((server.codex_home / "config.toml").read_text())
        assert result["features"]["memories"] is False
        assert result["sqlite_home"] == str(server.codex_home)
        assert "USER RULE" in result["developer_instructions"]
        assert "AGENT RULE" in result["developer_instructions"]
        assert result["developer_instructions"].count("PROJECT LESSON") == 1
        assert server.config_overrides[-2:] == [
            "features.memories=false",
            f'sqlite_home="{server.codex_home}"',
        ]
        assert (source / "config.toml").read_text() == original
    finally:
        await server.close()
