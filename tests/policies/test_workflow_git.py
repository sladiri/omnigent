"""Workflow Git permissions use real repository metadata without advancing stages."""

import copy
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from omnigent.workflows.controller import STATE_KEY, _worker_policy
from tests.tools.test_workflow_operations import repository as repository


@pytest.fixture
def worker_git(repository, monkeypatch):
    pending = {"title": "one/implement/1", "stage": "one/implement", "child_id": "child"}
    state = {
        "status": "running",
        "current": "one/implement",
        "definition": {"repository": str(repository), "publish": False},
        "pending": pending,
        "candidates": [{"id": "one", "workspace": str(repository)}],
    }
    sessions = {
        "root": SimpleNamespace(session_state={STATE_KEY: state}),
        "child": SimpleNamespace(
            id="child",
            workspace=str(repository),
            parent_conversation_id="root",
            title="worker:one/implement/1",
        ),
    }
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )

    def call(command, *, tool="exec_command", prefix=False):
        args = {"cmd" if tool in {"exec_command", "shell_command"} else "command": command}
        if prefix:
            args[next(iter(args))] = f"cd {shlex.quote(str(repository))} && " + command
        else:
            args["workdir"] = str(repository)
        before = copy.deepcopy(state)
        verdict = _worker_policy(
            {
                "type": "tool_call",
                "target": tool,
                "data": {"name": tool, "arguments": args},
                "context": {"conversation_id": "child"},
            },
            "root",
            "child",
        )
        assert state == before
        return verdict

    return state, call


@pytest.mark.parametrize("tool", ["exec_command", "shell_command", "sys_os_shell", "Bash"])
@pytest.mark.parametrize("prefix", [False, True])
def test_worker_can_create_adjacent_worktree(repository, worker_git, tool, prefix):
    _, call = worker_git
    command = "/usr/bin/env git worktree add -b new ../repo-new HEAD"
    verdict = call(command, tool=tool, prefix=prefix)
    assert verdict["result"] == "ALLOW", verdict
    subprocess.run(shlex.split(command), cwd=repository, check=True, capture_output=True)
    assert (repository.parent / "repo-new/hello.txt").read_text() == "hello\n"
    # Creation does not change the worker's assigned edit scope.
    assert call("cd ../repo-new && pwd")["result"] == "DENY"


def test_worker_worktree_limits_and_read_only_ancestry(repository, worker_git):
    state, call = worker_git
    for command in (
        "git merge-base --is-ancestor HEAD main",
        "git --no-pager log --oneline HEAD..main && git merge-base HEAD main",
        "git worktree list --porcelain",
    ):
        assert call(command)["result"] == "ALLOW", command
    for command in (
        "git worktree add --force ../new",
        "git worktree add -B main ../new",
        "git worktree add ../../new",
        "git worktree remove ../new",
        "git worktree move ../new ../other",
        "git merge main",
        "git reset --hard HEAD",
        "git rebase main",
        "git cherry-pick HEAD",
        "git commit -am changed",
        "git push origin main",
        "git merge-base HEAD main && git merge main",
        "git worktree add ../new && git push origin main",
        "git --work-tree=../outside status",
        "git --work-tree ../outside status",
        "git -C . --work-tree=../outside status",
        "git --work-tree=. worktree add ../new",
    ):
        assert call(command)["result"] == "DENY", command
    (repository.parent / "existing").mkdir()
    (repository.parent / "escape").symlink_to(repository.parent.parent, target_is_directory=True)
    for destination in ("../existing", "../escape/new"):
        assert call("git worktree add " + destination)["result"] == "DENY"
    for stage in (
        "implement",
        "fix-a",
        "fix-b",
        "fix-c",
        "review-a",
        "verify-a",
        "review-b-standards",
        "review-b-spec",
        "review-c",
        "final-b-spec",
    ):
        state["pending"]["stage"] = "one/" + stage
        expected = "ALLOW" if stage in {"implement", "fix-a", "fix-b", "fix-c"} else "DENY"
        assert call("git worktree add --detach ../new")["result"] == expected, stage
        assert call("git merge-base --is-ancestor HEAD main")["result"] == "ALLOW"
    for stage in ("explore", "compare-results"):
        state["pending"]["stage"] = stage
        assert call("git worktree add ../new")["result"] == "DENY"
    state["pending"]["stage"] = "one/implement"
    state["status"] = "blocked"
    assert call("git worktree add ../new")["result"] == "DENY"
    state["status"] = "running"
    state["pending"]["stopped"] = True
    assert call("git worktree add ../new")["result"] == "DENY"


@pytest.mark.parametrize("tool", ["exec_command", "sys_os_shell", "Bash"])
@pytest.mark.parametrize(
    "command",
    [
        "git --no-pager show HEAD:deploy/k8s/tests/push-render.sh",
        "git --no-pager diff -- deploy/k8s/charts/push",
        "git log --grep='commit or reset' --oneline",
        "git status && rg 'commit|push|reset' README.md",
        "/usr/bin/env git -c color.ui=false --no-pager show HEAD:docs/rebase.md",
        "bash -lc 'git --no-pager show HEAD:deploy/k8s/tests/push-render.sh'",
    ],
)
def test_worker_git_reads_do_not_classify_arguments_as_commands(worker_git, tool, command):
    _, call = worker_git
    assert call(command, tool=tool, prefix=True)["result"] == "ALLOW"


@pytest.mark.parametrize(
    "command",
    [
        "git --no-pager diff -- README.md && git commit -am change",
        "bash -lc 'git status && git reset --hard HEAD'",
        "/usr/bin/env git -c color.ui=false push origin main",
        "/usr/bin/git --no-pager cherry-pick HEAD",
        "git -C . merge main",
        "git log --oneline; git rebase main",
        "bash -lc 'git status' && git commit -am change",
        "if true; then git commit -am change; fi",
        "git -c alias.save='!git commit -am change' save",
        "git -c core.pager='git commit -am change' log",
        "echo $(git commit -am change)",
        "git status \\\n--short",
        "cat <<EOF\nhello\nEOF",
    ],
)
def test_worker_still_denies_git_mutations_after_reads(worker_git, command):
    _, call = worker_git
    assert call(command, prefix=True)["result"] == "DENY"
