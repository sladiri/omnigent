"""The shipped worker can read installed skills without opening unrelated files."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.spec.parser import parse
from omnigent.workflows.controller import STATE_KEY, _worker_policy

BUNDLE = Path(__file__).resolve().parents[2] / "examples/workflow_controller/agents/worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("installation", [".agents", ".codex", ".claude"])
async def test_worker_reads_symlinked_skills_without_write_or_sibling_access(
    tmp_path, monkeypatch, installation
):
    home = tmp_path / "home"
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    installs = home / installation / "skills"
    installs.mkdir(parents=True)
    source = tmp_path / "skill-repository"
    source.mkdir()
    secret = source / "unrelated.txt"
    secret.write_text("not a skill")
    spec = parse(BUNDLE)
    pending = {"stage": "one/review-a", "title": "review", "child_id": "child"}
    state = {
        "status": "running",
        "definition": {"repository": str(workspace)},
        "current": "one/review-a",
        "pending": pending,
        "candidates": [{"id": "one", "workspace": str(workspace)}],
    }
    sessions = {
        "root": SimpleNamespace(session_state={STATE_KEY: state}),
        "child": SimpleNamespace(id="child", parent_conversation_id="root", title="worker:review"),
    }
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )

    def permitted(tool, path):
        return _worker_policy(
            {"type": "tool_call", "target": tool, "data": {"arguments": {"path": str(path)}}},
            "root",
            "child",
        )["result"]

    async def read(path):
        return await _execute_os_env_tool(
            "sys_os_read", {"path": str(path)}, agent_spec=spec, runner_workspace=workspace
        )

    extra = {
        ".agents": "caveman-commit",
        ".codex": ".system/skill-creator",
        ".claude": "skill-review-my-pr",
    }
    for name, stage in [
        ("ponytail", "one/review-a"),
        ("ponytail-review", "one/review-c"),
        ("code-review", "one/review-b-spec"),
        ("compare-solutions", "explore"),
        (extra[installation], "one/implement"),
    ]:
        installed = source / name
        installed.mkdir(parents=True)
        skill = installed / "SKILL.md"
        skill.write_text("required skill " + name)
        reference = installed / "reference.md"
        reference.write_text("required reference " + name)
        (installs / name).parent.mkdir(parents=True, exist_ok=True)
        (installs / name).symlink_to(installed, target_is_directory=True)
        pending["stage"] = stage
        for path in [skill, installs / name / "SKILL.md", reference]:
            assert permitted("sys_os_read", path) == "ALLOW"
            result = await read(path)
            assert "required" in result and "Error:" not in result, result
        escape = installed / "escape"
        escape.symlink_to(secret)
        assert permitted("sys_os_read", escape) == "DENY"
        assert "no sandbox read grant" in await read(escape)
        for stage in ["one/review-a", "one/implement"]:
            pending["stage"] = stage
            assert permitted("sys_os_write", skill) == "DENY"
        result = await _execute_os_env_tool(
            "sys_os_write",
            {"path": str(skill), "content": "changed"},
            agent_spec=spec,
            runner_workspace=workspace,
        )
        assert "no sandbox write grant" in result
        assert skill.read_text() == "required skill " + name
    assert "no sandbox read grant" in await read(secret)
    unlisted = installs / "new-unlisted-skill"
    unlisted.mkdir()
    (unlisted / "SKILL.md").write_text("Unlisted future skill")
    assert "no sandbox read grant" in await read(unlisted / "SKILL.md")
    state["status"] = "waiting-user"
    assert permitted("sys_os_read", skill) == "DENY"
