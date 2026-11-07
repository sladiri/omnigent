"""Host workflow operations against isolated real Git worktrees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.workflows import operations


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    operations._git(repo, "init", "--initial-branch=main")
    operations._git(repo, "config", "user.name", "Workflow Test")
    operations._git(repo, "config", "user.email", "workflow@example.invalid")
    (repo / "hello.txt").write_text("hello\n")
    (repo / ".gitignore").write_text("ignored\n")
    operations._git(repo, "add", ".")
    operations._git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    return repo


def _definition(repository: Path) -> dict:
    return {
        "repository": str(repository),
        "task": "Improve hello",
        "run_id": "test-run",
        "worktree_setup": False,
        "candidates": [
            {"id": "alpha", "task": "Add greeting"},
            {"id": "beta", "task": "Add farewell"},
        ],
    }


def _snapshot(repository: Path) -> dict:
    return json.loads(operations.workflow_snapshot(str(repository)))


def test_snapshot_covers_working_contents_and_is_stable_across_commit(repository: Path) -> None:
    before = _snapshot(repository)
    (repository / "ignored").write_text("does not count")
    assert _snapshot(repository)["revision"] == before["revision"]
    (repository / "hello.txt").write_text("new hello\n")
    unstaged = _snapshot(repository)
    assert unstaged["revision"] != before["revision"]
    (repository / "new.txt").write_text("untracked\n")
    (repository / " leading-name ").write_text("whitespace name\n")
    untracked = _snapshot(repository)
    assert untracked["revision"] != unstaged["revision"]
    operations._git(repository, "add", "--all")
    assert _snapshot(repository)["revision"] == untracked["revision"]
    operations._git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "change")
    assert _snapshot(repository)["revision"] == untracked["revision"]
    (repository / "hello.txt").chmod(0o755)
    assert _snapshot(repository)["revision"] != untracked["revision"]
    (repository / "new.txt").unlink()
    deleted = _snapshot(repository)["revision"]
    operations._git(repository, "add", "--all")
    operations._git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "delete")
    assert _snapshot(repository)["revision"] == deleted


def test_prepare_recovers_owned_worktrees_and_preserves_candidate_changes(
    repository: Path,
) -> None:
    definition = _definition(repository)
    prepared = json.loads(operations.workflow_prepare(definition))
    alpha, beta = prepared["candidates"]
    alpha_path, beta_path = Path(alpha["workspace"]), Path(beta["workspace"])
    assert alpha_path != beta_path
    assert _snapshot(alpha_path)["head"] == prepared["baseline"]
    (alpha_path / "hello.txt").write_text("candidate work\n")
    assert json.loads(operations.workflow_prepare(definition)) == prepared
    assert (alpha_path / "hello.txt").read_text() == "candidate work\n"
    assert (beta_path / "hello.txt").read_text() == "hello\n"
    assert (repository / "hello.txt").read_text() == "hello\n"
    with pytest.raises(ValueError, match="definition changed"):
        operations.workflow_prepare({**definition, "task": "Something else"})


def test_review_prepare_preserves_current_implementation_and_recovers(
    repository: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "current-worktree"
    operations._git(repository, "worktree", "add", "-b", "feature/current", str(workspace))
    (workspace / "committed.txt").write_text("already implemented\n")
    operations._git(workspace, "add", ".")
    operations._git(workspace, "-c", "commit.gpgsign=false", "commit", "-m", "implementation")
    (workspace / "hello.txt").write_text("staged implementation\n")
    operations._git(workspace, "add", ".")
    (workspace / "hello.txt").write_text("unstaged implementation\n")
    (workspace / "new.txt").write_text("untracked implementation\n")
    before = _snapshot(workspace)
    index = operations._git(workspace, "diff", "--cached")
    trees = operations._git(repository, "worktree", "list", "--porcelain")
    definition = {
        **_definition(workspace),
        "workflow": "review-fix",
        "baseline": "main",
        "candidates": [{"id": "current", "task": "Review current implementation"}],
    }
    prepared = json.loads(operations.workflow_prepare(definition))
    candidate = prepared["candidates"][0]
    assert candidate["workspace"] == str(workspace)
    assert candidate["branch"] == "feature/current"
    assert candidate["baseline"] == operations._git(repository, "rev-parse", "main")
    assert candidate["baseline"] != before["head"]
    assert json.loads(operations.workflow_prepare(definition)) == prepared
    assert _snapshot(workspace) == before
    assert operations._git(workspace, "diff", "--cached") == index
    assert operations._git(repository, "worktree", "list", "--porcelain") == trees
    operations._git(workspace, "switch", "-c", "other")
    with pytest.raises(ValueError, match="Review worktree or branch changed"):
        operations.workflow_prepare(definition)


@pytest.mark.parametrize("invalid", ["multiple", "branch", "workspace", "detached"])
def test_review_prepare_refuses_ambiguous_target(repository: Path, invalid: str) -> None:
    definition = {**_definition(repository), "workflow": "review-fix"}
    if invalid != "multiple":
        definition["candidates"] = definition["candidates"][:1]
    if invalid in {"branch", "workspace"}:
        definition["candidates"][0][invalid] = "another"
    if invalid == "detached":
        operations._git(repository, "checkout", "--detach")
    with pytest.raises(ValueError, match="Review"):
        operations.workflow_prepare(definition)


def test_revised_design_uses_a_new_worktree_and_preserves_prior_work(repository: Path) -> None:
    definition = _definition(repository)
    first = {**definition, "run_id": "a" * 32 + "-designs-1"}
    second = {**definition, "run_id": "a" * 32 + "-designs-2"}
    before = json.loads(operations.workflow_prepare(first))
    prior = Path(before["candidates"][0]["workspace"])
    (prior / "hello.txt").write_text("prior design work\n")
    after = json.loads(operations.workflow_prepare(second))
    assert {c["workspace"] for c in before["candidates"]}.isdisjoint(
        c["workspace"] for c in after["candidates"]
    )
    assert (prior / "hello.txt").read_text() == "prior design work\n"
    assert all(
        (Path(c["workspace"]) / "hello.txt").read_text() == "hello\n" for c in after["candidates"]
    )
    assert json.loads(operations.workflow_prepare(second)) == after


def test_prepare_refuses_dirty_baseline_and_unowned_branch(repository: Path) -> None:
    definition = _definition(repository)
    (repository / "hello.txt").write_text("unrelated user work")
    with pytest.raises(ValueError, match="unrelated changes"):
        operations.workflow_prepare(definition)
    (repository / "hello.txt").write_text("hello\n")
    for duplicate in (
        {"branch": "workflow/duplicate"},
        {"workspace": str(repository.parent / "duplicate")},
        {"workspace": str(repository)},
    ):
        invalid = {
            **definition,
            "candidates": [{**candidate, **duplicate} for candidate in definition["candidates"]],
        }
        with pytest.raises(ValueError, match="distinct branches and isolated worktrees"):
            operations.workflow_prepare(invalid)
    operations._git(repository, "branch", "workflow/test-run/alpha")
    with pytest.raises(ValueError, match="ownership"):
        operations.workflow_prepare(definition)


def test_prepare_runs_setup_helper_before_return(repository: Path, tmp_path: Path) -> None:
    helper = tmp_path / "setup.py"
    helper.write_text(
        "import pathlib,sys\n(pathlib.Path(sys.argv[1]) / '.setup').write_text('ready')\n"
    )
    definition = {**_definition(repository), "worktree_setup": str(helper)}
    prepared = json.loads(operations.workflow_prepare(definition))
    assert all(
        (Path(candidate["workspace"]) / ".setup").read_text() == "ready"
        for candidate in prepared["candidates"]
    )


def test_prepare_reuses_explicit_existing_and_moved_owned_worktree(
    repository: Path, tmp_path: Path
) -> None:
    existing = tmp_path / "unrelated-directory-name"
    operations._git(repository, "worktree", "add", "-b", "feature/greeting", str(existing))
    definition = {
        **_definition(repository),
        "candidates": [
            {
                "id": "alpha",
                "task": "Greeting",
                "branch": "feature/greeting",
                "workspace": str(existing),
            }
        ],
    }
    prepared = json.loads(operations.workflow_prepare(definition))
    assert prepared["candidates"][0]["workspace"] == str(existing)
    (existing / "hello.txt").write_text("owned work")
    moved = tmp_path / "moved-candidate"
    operations._git(repository, "worktree", "move", str(existing), str(moved))
    resumed = json.loads(operations.workflow_prepare(definition))
    assert resumed["candidates"][0]["workspace"] == str(moved)
    assert (moved / "hello.txt").read_text() == "owned work"


@pytest.mark.parametrize("lost_response_stage", ["commit", "push", "pr"])
def test_publish_reconciles_commit_push_and_created_pr_after_lost_response(
    repository: Path, monkeypatch: pytest.MonkeyPatch, lost_response_stage: str
) -> None:
    prepared = json.loads(operations.workflow_prepare(_definition(repository)))
    workspace = Path(prepared["candidates"][0]["workspace"])
    operations._git(workspace, "remote", "add", "origin", "git@github.com:upstream/test.git")
    operations._git(
        workspace, "remote", "set-url", "--push", "origin", "git@github.com:example/test.git"
    )
    (workspace / "hello.txt").write_text("reviewed change\n")
    revision = _snapshot(workspace)["revision"]
    original = operations._run
    calls = []
    published = {"pushed": False, "pr": False, "state": "OPEN"}
    monkeypatch.setattr(operations.shutil, "which", lambda _: "/test/pre-commit")

    def run(path, *args, env=None):
        calls.append(args)
        if args[:3] == ("git", "--no-pager", "commit"):
            assert env.get("GPG_TTY") is None
            result = original(
                path,
                "git",
                "-c",
                "commit.gpgsign=false",
                "--no-pager",
                "commit",
                "-s",
                "-m",
                args[-1],
            )
            if lost_response_stage == "commit":
                raise RuntimeError("lost response after commit")
            return result
        if args[:3] == ("git", "--no-pager", "verify-commit"):
            return ""
        if args[:3] == ("git", "--no-pager", "push"):
            published["pushed"] = True
            if lost_response_stage == "push":
                raise RuntimeError("lost response after push")
            return ""
        if args[0] == "/test/pre-commit":
            return ""
        if args[:2] == ("gh", "api"):
            assert args[2].startswith("repos/example/test/")
            head = original(path, "git", "rev-parse", "HEAD")
            if "matching-refs" in args[2]:
                return json.dumps(
                    [
                        {
                            "ref": f"refs/heads/{prepared['candidates'][0]['branch']}",
                            "object": {"sha": head},
                        }
                    ]
                    if published["pushed"]
                    else []
                )
            return json.dumps({"object": {"sha": head}})
        if args[:3] == ("gh", "pr", "list"):
            assert args[args.index("--repo") + 1] == "example/test"
            return json.dumps(
                [
                    {
                        "url": "https://github.com/example/test/pull/1",
                        "state": published["state"],
                        "headRefOid": original(path, "git", "rev-parse", "HEAD"),
                    }
                ]
                if published["pr"]
                else []
            )
        if args[:3] == ("gh", "pr", "create"):
            assert args[args.index("--repo") + 1] == "example/test"
            assert Path(args[-1]).read_text() == "Reviewed change and test evidence"
            published["pr"] = True
            if lost_response_stage == "pr":
                raise RuntimeError("lost response after PR creation")
            return "https://github.com/example/test/pull/1"
        return original(path, *args, env=env)

    monkeypatch.setattr(operations, "_run", run)
    monkeypatch.setattr(operations, "_desktop_environment", lambda _: {"DISPLAY": ":test"})
    params = (
        str(workspace),
        revision,
        "TEST-1 feat: improve hello",
        "Reviewed change and test evidence",
        "main",
    )
    with pytest.raises(RuntimeError, match="lost response"):
        operations.workflow_publish(*params)
    result = json.loads(operations.workflow_publish(*params))
    assert result["pr_url"] == "https://github.com/example/test/pull/1"
    assert json.loads(operations.workflow_publish(*params)) == result
    assert sum(args[:3] == ("git", "--no-pager", "commit") for args in calls) == 1
    assert sum(args[:3] == ("git", "--no-pager", "push") for args in calls) == 1
    assert sum(args[:3] == ("gh", "pr", "create") for args in calls) == 1
    operations._git(
        workspace, "remote", "set-url", "--push", "origin", "git@github.com:another/test.git"
    )
    with pytest.raises(ValueError, match="Publication parameters changed"):
        operations.workflow_publish(*params)
    operations._git(
        workspace, "remote", "set-url", "--push", "origin", "git@github.com:example/test.git"
    )
    published.update(state="CLOSED", pushed=False)
    with pytest.raises(ValueError, match="refusing to recreate"):
        operations.workflow_publish(*params)
    published["state"] = "MERGED"
    assert json.loads(operations.workflow_publish(*params)) == result
    assert not published["pushed"]
    assert sum(args[:3] == ("git", "--no-pager", "push") for args in calls) == 1
    assert sum(args[:3] == ("gh", "pr", "create") for args in calls) == 1
    (workspace / "hello.txt").write_text("unreviewed change")
    with pytest.raises(ValueError, match="Reviewed contents changed"):
        operations.workflow_publish(*params)


def test_publish_refuses_multiple_push_destinations(repository: Path) -> None:
    prepared = json.loads(operations.workflow_prepare(_definition(repository)))
    workspace = Path(prepared["candidates"][0]["workspace"])
    operations._git(workspace, "remote", "add", "origin", "git@github.com:example/test.git")
    for owner in ("example", "another"):
        operations._git(
            workspace,
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            f"git@github.com:{owner}/test.git",
        )
    (workspace / "hello.txt").write_text("reviewed change\n")
    snapshot = _snapshot(workspace)
    with pytest.raises(ValueError, match="single push destination"):
        operations.workflow_publish(
            str(workspace), snapshot["revision"], "TEST-1 feat: improve hello", "Evidence", "main"
        )
    assert _snapshot(workspace) == snapshot


@pytest.mark.parametrize("configured", [False, True])
def test_publish_requires_pre_commit_only_when_configured(repository, monkeypatch, configured):
    prepared = json.loads(operations.workflow_prepare(_definition(repository)))
    workspace = Path(prepared["candidates"][0]["workspace"])
    operations._git(workspace, "remote", "add", "origin", "git@github.com:example/test.git")
    (workspace / "hello.txt").write_text("reviewed change\n")
    if configured:
        (workspace / ".pre-commit-config.yaml").write_text("repos: []\n")
    revision = _snapshot(workspace)["revision"]
    original = operations._run
    monkeypatch.setattr(operations.shutil, "which", lambda _: None)
    monkeypatch.setattr(operations, "_desktop_environment", lambda _: {})

    def run(path, *args, env=None):
        if args[:3] == ("git", "--no-pager", "commit"):
            raise RuntimeError("Reached native Git commit hooks")
        assert args[0] != "gh"
        return original(path, *args, env=env)

    monkeypatch.setattr(operations, "_run", run)
    error = ValueError if configured else RuntimeError
    message = "Install pre-commit" if configured else "Reached native Git commit hooks"
    with pytest.raises(error, match=message):
        operations.workflow_publish(
            str(workspace), revision, "TEST-1 feat: improve hello", "Reviewed change", "main"
        )
    assert _snapshot(workspace)["revision"] == revision


def test_publish_checks_new_files_before_committing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repository / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: reject-python\n"
        "        name: reject-python\n"
        "        language: system\n"
        "        entry: /usr/bin/env python3 -c 'raise SystemExit(1)'\n"
        "        types: [python]\n"
    )
    operations._git(repository, "add", "--all")
    operations._git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "add hook")
    prepared = json.loads(operations.workflow_prepare(_definition(repository)))
    workspace = Path(prepared["candidates"][0]["workspace"])
    operations._git(workspace, "remote", "add", "origin", "git@github.com:example/test.git")
    (workspace / "new.py").write_text("new source\n")
    snapshot = _snapshot(workspace)
    original = operations._run

    def run(path, *args, env=None):
        assert args[:3] != ("git", "--no-pager", "commit"), "Unchecked file reached commit"
        return original(path, *args, env=env)

    monkeypatch.setattr(operations, "_run", run)
    monkeypatch.setattr(operations, "_desktop_environment", lambda _: {"DISPLAY": ":test"})
    monkeypatch.setattr(
        operations.shutil, "which", lambda _: str(Path(sys.executable).with_name("pre-commit"))
    )
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit"))
    with pytest.raises(subprocess.CalledProcessError) as failed:
        operations.workflow_publish(
            str(workspace), snapshot["revision"], "TEST-1 feat: improve hello", "Evidence", "main"
        )
    assert b"reject-python" in failed.value.stdout
    assert _snapshot(workspace) == snapshot


def test_publish_stops_if_commit_hook_changes_reviewed_tree(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = json.loads(operations.workflow_prepare(_definition(repository)))
    workspace = Path(prepared["candidates"][0]["workspace"])
    operations._git(workspace, "remote", "add", "origin", "git@github.com:example/test.git")
    (workspace / "hello.txt").write_text("reviewed change\n")
    revision = _snapshot(workspace)["revision"]
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/usr/bin/env bash\nprintf 'unreviewed hook change\\n' > hello.txt\ngit add hello.txt\n"
    )
    hook.chmod(0o755)
    operations._git(workspace, "config", "core.hooksPath", str(hooks))
    original = operations._run

    def run(path, *args, env=None):
        if args[:3] == ("git", "--no-pager", "commit"):
            return original(path, "git", "-c", "commit.gpgsign=false", "commit", "-m", args[-1])
        if args[0] == "/test/pre-commit" or args[:3] == ("git", "--no-pager", "verify-commit"):
            return ""
        assert args[0] != "gh" and args[:3] != ("git", "--no-pager", "push")
        return original(path, *args, env=env)

    monkeypatch.setattr(operations, "_run", run)
    monkeypatch.setattr(operations.shutil, "which", lambda _: "/test/pre-commit")
    monkeypatch.setattr(operations, "_desktop_environment", lambda _: {"DISPLAY": ":test"})
    with pytest.raises(ValueError, match="Commit hooks changed the reviewed tree"):
        operations.workflow_publish(
            str(workspace), revision, "TEST-1 feat: improve hello", "Reviewed change", "main"
        )
    assert not _snapshot(workspace)["dirty"]
    assert _snapshot(workspace)["revision"] != revision


def test_desktop_environment_reads_session_and_removes_terminal_pinentry(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GPG_TTY", "/dev/pts/test")
    monkeypatch.setattr(
        operations,
        "_run",
        lambda *_: (
            "DISPLAY=:test\nXAUTHORITY=/tmp/rotating-auth\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/test/bus"
        ),
    )
    environment = operations._desktop_environment(repository)
    assert environment["DISPLAY"] == ":test"
    assert environment["XAUTHORITY"] == "/tmp/rotating-auth"
    assert "GPG_TTY" not in environment
    assert os.environ["GPG_TTY"] == "/dev/pts/test"


def test_draft_branch_reaches_real_worktree(repository):
    from omnigent.workflows.controller import draft_start_message

    draft = json.loads(
        draft_start_message(
            "implement-review-fix",
            str(repository),
            "Task",
            "main",
            "main",
            branch="feature/lcs-1670",
        )
    )
    definition = json.loads(draft["command"].removeprefix("/workflow start "))
    definition.update(run_id="branch-draft", worktree_setup=False)
    prepared = json.loads(operations.workflow_prepare(definition))
    candidate = prepared["candidates"][0]
    assert candidate["branch"] == "feature/lcs-1670"
    assert (
        operations._git(Path(candidate["workspace"]), "branch", "--show-current")
        == "feature/lcs-1670"
    )
    assert operations._git(repository, "branch", "--show-current") == "main"


@pytest.fixture
def review_pr(repository, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    operations._git(tmp_path, "clone", "--no-hardlinks", str(repository), str(checkout))
    operations._git(checkout, "remote", "set-url", "origin", "git@github.com:org/repo.git")
    operations._git(
        checkout, "config", f"url.{repository}.insteadOf", "git@github.com:org/repo.git"
    )
    baseline = operations._git(repository, "rev-parse", "HEAD")
    operations._git(repository, "checkout", "-b", "feature/review")
    (repository / "hello.txt").write_text("PR change\n")
    operations._git(repository, "commit", "-am", "PR", "--no-gpg-sign")
    head = operations._git(repository, "rev-parse", "HEAD")
    operations._git(repository, "update-ref", "refs/pull/303/head", head)
    operations._git(repository, "checkout", "main")
    (repository / "new-base.txt").write_text("later target change\n")
    operations._git(repository, "add", ".")
    operations._git(repository, "commit", "-m", "target", "--no-gpg-sign")
    metadata = {
        "url": "https://github.com/org/repo/pull/303",
        "headRefName": "feature/review",
        "headRefOid": head,
        "baseRefName": "main",
        "isCrossRepository": False,
        "baseRefOid": operations._git(repository, "rev-parse", "HEAD"),
    }
    run = operations._run
    setup = []

    def local_run(workspace, *args, **kwargs):
        if args[0] == "gh":
            assert args[1:3] == ("pr", "view")
            return json.dumps(metadata)
        return run(workspace, *args, **kwargs)

    monkeypatch.setattr(operations, "_run", local_run)
    monkeypatch.setattr(operations, "_setup_worktree", lambda path, definition: setup.append(path))
    return checkout, metadata, baseline, setup


def test_review_checkout_fetches_pr_reuses_worktree_and_keeps_base_untouched(review_pr):
    repository, metadata, baseline, setup = review_pr
    (repository / "unrelated.txt").write_text("Preserve local work\n")
    before = _snapshot(repository)
    result = json.loads(operations.workflow_review_checkout(str(repository), metadata["url"]))
    checkout = Path(result["repository"])
    assert checkout != repository
    assert result["baseline"] == baseline
    assert result["base"] == "main"
    assert result["head"] == metadata["headRefOid"]
    assert result["branch"] == "feature/review"
    assert setup == [checkout]
    assert (checkout / "hello.txt").read_text() == "PR change\n"
    assert _snapshot(repository) == before
    assert (
        json.loads(operations.workflow_review_checkout(str(repository), metadata["url"])) == result
    )
    (checkout / "hello.txt").write_text("unrelated local change\n")
    with pytest.raises(ValueError, match="dirty"):
        operations.workflow_review_checkout(str(repository), metadata["url"])
    assert (checkout / "hello.txt").read_text() == "unrelated local change\n"


def test_review_checkout_rejects_wrong_repo_and_stale_branch(review_pr):
    repository, metadata, _, setup = review_pr
    with pytest.raises(ValueError, match="repository"):
        operations.workflow_review_checkout(
            str(repository), "https://github.com/other/repo/pull/303"
        )
    assert not setup
    operations._git(repository, "branch", "feature/review", "HEAD")
    with pytest.raises(ValueError, match="differs"):
        operations.workflow_review_checkout(str(repository), metadata["url"])
    assert not setup


def test_review_checkout_fork_uses_separate_branch_and_preserves_dangling_path(review_pr):
    repository, metadata, _, _ = review_pr
    metadata["isCrossRepository"] = True
    path = repository.parent / f"{repository.name}-pr-303"
    path.symlink_to(repository.parent / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="Unregistered"):
        operations.workflow_review_checkout(str(repository), metadata["url"])
    assert path.is_symlink()
    path.unlink()
    result = json.loads(operations.workflow_review_checkout(str(repository), metadata["url"]))
    assert result["branch"] == "workflow/review-pr-303"


def test_review_checkout_rejects_refs_changed_during_fetch(review_pr):
    repository, metadata, _, setup = review_pr
    metadata["headRefOid"] = "f" * 40
    with pytest.raises(ValueError, match="changed during fetch"):
        operations.workflow_review_checkout(str(repository), metadata["url"])
    assert not setup
    assert len(operations._worktrees(repository)) == 1
