"""Source-monorepo reads remain bounded and do not grant workflow writes."""

import copy
import json
from types import SimpleNamespace

import pytest

from omnigent.spec.parser import parse
from omnigent.workflows.controller import STATE_KEY, _worker_policy
from omnigent.workflows.operations import _git
from omnigent.workflows.repository import workflow_read
from tests.tools.test_workflow_operations import repository as repository


@pytest.mark.parametrize(
    "stage", ["implement", "fix-a", "review-a", "verify-a", "explore", "compare-results"]
)
@pytest.mark.parametrize("linked_source", [False, True])
def test_source_monorepo_read_from_subfolder(
    repository, tmp_path, monkeypatch, stage, linked_source
):
    subfolder = repository / "services" / "homekit"
    subfolder.mkdir(parents=True)
    instructions = repository / ".codex" / "config.toml"
    instructions.parent.mkdir()
    instructions.write_text('developer_instructions = "Read shared guidance"\n')
    (repository / "CLAUDE.local.md").write_text("Shared instructions\nSecond line\n")
    candidate = tmp_path / "candidate"
    _git(repository, "worktree", "add", "-b", "candidate", str(candidate))
    if linked_source:
        subfolder = candidate / "services" / "homekit"
        subfolder.mkdir(parents=True)
        (candidate / "CLAUDE.local.md").write_text("Candidate instructions\nSecond line\n")
    source = candidate if linked_source else repository
    other = tmp_path / "other"
    _git(repository, "worktree", "add", "-b", "other", str(other))
    outside = tmp_path / "unrelated.txt"
    outside.write_text("Outside the repository")
    (repository / "escape").symlink_to(outside)
    if linked_source:
        (candidate / "escape").symlink_to(outside)
    pending = {
        "stage": stage if stage in {"explore", "compare-results"} else "one/" + stage,
        "title": "assigned",
        "child_id": "child",
    }
    state = {
        "status": "running",
        "definition": {"repository": str(subfolder)},
        "current": pending["stage"],
        "pending": pending,
        "candidates": [{"id": "one", "workspace": str(candidate)}],
    }
    sessions = {
        "root": SimpleNamespace(session_state={STATE_KEY: state}),
        "child": SimpleNamespace(
            id="child", parent_conversation_id="root", title="worker:assigned"
        ),
    }
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )

    def call(tool, **args):
        return _worker_policy(
            {"type": "tool_call", "target": tool, "data": {"arguments": args}}, "root", "child"
        )

    before = copy.deepcopy(state)
    for path, expected in [
        (str(instructions), "Read shared guidance"),
        (str(repository / "CLAUDE.local.md"), "Shared instructions"),
        ("CLAUDE.local.md", "Candidate instructions" if linked_source else "Shared instructions"),
        ("hello.txt", "hello"),
    ]:
        allowed = call("workflow_read", repository=str(tmp_path), path=path)
        assert allowed["result"] == "ALLOW"
        assert allowed["data"]["repository"] == str(subfolder)
        result = json.loads(workflow_read(**allowed["data"]))
        assert expected in result["content"]
        assert result["repository"] == str(source)
    assert "services" in json.loads(workflow_read(str(subfolder)))["entries"]
    assert (
        json.loads(workflow_read(str(subfolder), "CLAUDE.local.md", offset=2, limit=1))[
            "content"
        ].strip()
        == "Second line"
    )
    forbidden = [
        str(outside),
        "../unrelated.txt",
        "escape",
        str(other / "hello.txt"),
        str(repository / "escape"),
    ]
    if not linked_source:
        forbidden.append(str(candidate / "hello.txt"))
    for path in forbidden:
        with pytest.raises(ValueError, match="inside the source repository"):
            workflow_read(str(subfolder), path)
    assert call("sys_os_write", path=str(instructions), content="overwrite")["result"] == "DENY"
    assert state == before
    for status in ("waiting-user", "blocked", "complete", "cancelled"):
        state["status"] = status
        assert call("workflow_read", repository="", path=str(instructions))["result"] == "DENY"
    state["status"] = "running"
    pending["stopped"] = True
    assert call("workflow_read", repository="", path=str(instructions))["result"] == "DENY"
    assert instructions.read_text() == 'developer_instructions = "Read shared guidance"\n'


def test_read_tool_shipped_in_controller_and_worker():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "examples/workflow_controller"
    for bundle in (root, root / "agents/worker"):
        spec = parse(bundle)
        assert any(tool.name == "workflow_read" for tool in spec.local_tools)
