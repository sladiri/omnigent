"""Journal resolution and fixed Beads argv against real Git worktrees."""

import json

import pytest

from omnigent.workflows import journal
from omnigent.workflows.operations import _git
from tests.tools.test_workflow_operations import repository as repository


@pytest.fixture
def linked_journal(repository, tmp_path, monkeypatch):
    beads = repository / ".beads"
    beads.mkdir()
    (beads / "beads.db").touch()
    (beads / "PRIME.md").write_text("Record activity, not approvals.")
    candidate = tmp_path / "candidate"
    _git(repository, "worktree", "add", "-b", "candidate", str(candidate))
    calls = []

    def run(cwd, *args, env):
        assert cwd == repository
        assert args[:4] == ("br", "--db", str(beads / "beads.db"), "--json")
        assert not any(k.startswith(("BEADS_", "BR_")) for k in env)
        calls.append(args[4:])
        return json.dumps([{"id": "test-1", "labels": ["session", "agent-journal"]}])

    monkeypatch.setenv("BEADS_DB", "/wrong/project.db")
    monkeypatch.setattr(journal, "_run", run)
    return candidate, beads, calls


@pytest.mark.parametrize("location", ["root", "main-subfolder", "linked-subfolder"])
def test_existing_main_journal_guidance_and_operations(linked_journal, location):
    candidate, beads, calls = linked_journal
    workspace = candidate
    if location != "root":
        workspace = (beads.parent if location == "main-subfolder" else candidate) / "services/app"
        workspace.mkdir(parents=True)
    result = json.loads(journal.workflow_journal(str(workspace), "instructions"))
    assert result["repository"] == str(beads.parent)
    assert result["instructions"] == "Record activity, not approvals."
    text = "[change] git push; $(touch /tmp/not-executed) 'quoted'\nnext line"
    for action in ("list", "show", "create", "claim", "comment", "close"):
        journal.workflow_journal(
            str(workspace),
            action,
            "test-1",
            "Session: journal (2026-11-03)" if action == "create" else text,
        )
    assert ("comments", "add", "test-1", "--message", text) in calls
    assert ("update", "test-1", "--claim") in calls
    assert ("close", "test-1", "--reason", text) in calls
    assert not (candidate / ".beads").exists()


@pytest.mark.parametrize("subfolder", [False, True])
def test_local_journal_precedence(linked_journal, subfolder):
    candidate, _, _ = linked_journal
    (candidate / ".beads").mkdir()
    (candidate / ".beads/PRIME.md").write_text("Local journal")
    workspace = candidate / "services/app" if subfolder else candidate
    workspace.mkdir(parents=True, exist_ok=True)
    result = json.loads(journal.workflow_journal(str(workspace), "instructions"))
    assert result["repository"] == str(candidate)
    assert result["instructions"] == "Local journal"


@pytest.mark.parametrize("action", ["init", "delete", "sync", "shell", "update"])
def test_disallowed_operations(linked_journal, action):
    candidate, _, calls = linked_journal
    with pytest.raises(ValueError, match="Unknown"):
        journal.workflow_journal(str(candidate), action)
    assert not calls


def test_missing_journal_does_not_initialize(repository):
    result = json.loads(
        journal.workflow_journal(str(repository), "create", text="Session: example")
    )
    assert result["available"] is False
    assert not (repository / ".beads").exists()


def test_reject_ordinary_issues_and_argument_injection(linked_journal, monkeypatch):
    candidate, _, calls = linked_journal
    for issue in ("--db", "test-1 --force", "../other"):
        with pytest.raises(ValueError, match="explicit"):
            journal.workflow_journal(str(candidate), "comment", issue, "text")
    assert not calls
    monkeypatch.setattr(journal, "_run", lambda *args, **kwargs: '[{"labels": ["bug"]}]')
    with pytest.raises(ValueError, match="Only session"):
        journal.workflow_journal(str(candidate), "comment", "test-1", "text")


def test_reject_database_and_guidance_escapes(linked_journal, tmp_path):
    candidate, beads, calls = linked_journal
    outside = tmp_path / "outside"
    outside.write_text("private")
    (beads / "PRIME.md").unlink()
    (beads / "PRIME.md").symlink_to(outside)
    with pytest.raises(ValueError, match="instructions"):
        journal.workflow_journal(str(candidate), "instructions")
    (beads / "metadata.json").write_text('{"database": "../outside"}')
    with pytest.raises(ValueError, match="local filename"):
        journal.workflow_journal(str(candidate), "list")
    assert not calls
