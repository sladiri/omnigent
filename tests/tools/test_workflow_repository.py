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
        allowed = call("workflow_read", repository="", path=path)
        assert allowed["result"] == "ALLOW"
        assert allowed["data"]["repository"] == str(subfolder)
        result = json.loads(workflow_read(**allowed["data"]))
        assert expected in result["content"]
        assert result["repository"] == str(source)
    assert call("workflow_read", repository=str(tmp_path), path="hello.txt")["result"] == "DENY"
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


@pytest.mark.parametrize(
    ("workflow", "stage"),
    [
        ("implement-review-fix", "one/implement"),
        ("review-fix", "one/review-a"),
        ("compare-review-fix", "explore"),
    ],
)
def test_approved_external_evidence_is_read_only(
    repository, tmp_path, monkeypatch, workflow, stage
):
    evidence = tmp_path / "support" / "report.md"
    evidence.parent.mkdir()
    evidence.write_text("Required evidence\n")
    unrelated = evidence.with_name("unrelated.md")
    unrelated.write_text("Not approved\n")
    workspace = tmp_path / "candidate"
    _git(repository, "worktree", "add", "-b", "candidate", str(workspace))
    state = {
        "status": "running",
        "definition": {
            "workflow": workflow,
            "repository": str(repository),
            "evidence_files": [str(evidence)],
        },
        "current": stage,
        "pending": {"stage": stage, "title": "assigned", "child_id": "child"},
        "candidates": [{"id": "one", "workspace": str(workspace)}],
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

    for tool, key in [("sys_os_read", "path"), ("Read", "file_path")]:
        assert call(tool, **{key: str(evidence)})["result"] == "ALLOW"
        assert call(tool, **{key: str(unrelated)})["result"] == "DENY"
    allowed = call("workflow_read", path=str(evidence), evidence_files=[str(unrelated)])
    assert allowed["result"] == "ALLOW"
    assert json.loads(workflow_read(**allowed["data"]))["content"].strip() == "Required evidence"
    denied = call("workflow_read", path=str(unrelated), evidence_files=[str(unrelated)])
    if denied["result"] == "ALLOW":
        with pytest.raises(ValueError):
            workflow_read(**denied["data"])
    for tool, key in [("sys_os_write", "path"), ("Write", "file_path"), ("Edit", "file_path")]:
        assert call(tool, **{key: str(evidence), "content": "overwrite"})["result"] == "DENY"
    escape = workspace / "escape"
    escape.symlink_to(unrelated)
    assert call("Read", file_path=str(escape))["result"] == "DENY"
    evidence.unlink()
    evidence.symlink_to(unrelated)
    assert call("Read", file_path=str(evidence))["result"] == "DENY"
    with pytest.raises(ValueError):
        workflow_read(**allowed["data"])
    evidence.unlink()
    evidence.write_text("Required evidence\n")
    state["status"] = "blocked"
    assert call("sys_os_read", path=str(evidence))["result"] == "DENY"
    assert call("workflow_read", path=str(evidence))["result"] == "DENY"
    assert evidence.read_text() == "Required evidence\n"


@pytest.mark.parametrize("workflow", ["implement-review-fix", "review-fix", "compare-review-fix"])
def test_draft_validates_external_evidence_before_start(repository, tmp_path, workflow):
    from omnigent.workflows.controller import STATE_KEY, draft_preview, draft_start_message

    evidence = tmp_path / "report.md"
    evidence.write_text("Evidence\n")
    link = tmp_path / "report-link.md"
    link.symlink_to(evidence)
    args = {
        "workflow": workflow,
        "repository": str(repository),
        "task": "Use the report",
        "baseline": "HEAD",
        "base": "main",
    }
    draft = json.loads(draft_start_message(**args, evidence_files=[str(link)]))
    assert draft["valid"] is True
    definition = json.loads(draft["command"].removeprefix("/workflow start "))
    assert definition["evidence_files"] == [str(evidence)]
    preview = draft_preview(
        {STATE_KEY: {"status": "idle", "draft": {"token": "draft", "definition": definition}}}
    )
    assert preview["draft"]["evidence_files"] == [str(evidence)]
    for invalid in (
        ["relative.md"],
        [str(tmp_path)],
        [str(tmp_path / "missing.md")],
        "not-a-list",
        [None],
        ["/nul\x00"],
    ):
        invalid_draft = json.loads(draft_start_message(**args, evidence_files=invalid))
        assert invalid_draft["valid"] is False
        assert "command" not in invalid_draft
