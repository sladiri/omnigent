"""Host-side Git operations for the policy-owned coding workflow."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from filelock import FileLock

_publication_fd: ContextVar[int | None] = ContextVar("workflow_publication_fd", default=None)


@contextmanager
def _publication_lock(path: Path) -> Iterator[None]:
    # A surviving signing/push child retains this lock if its tool process dies.
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = _publication_fd.set(handle.fileno())
        try:
            yield
        finally:
            _publication_fd.reset(token)


def _run(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run an argv command and keep hardware-key waits unbounded."""
    lock_fd = _publication_fd.get()
    completed = subprocess.run(
        ["/usr/bin/env", *args],
        cwd=workspace,
        env=env,
        check=True,
        capture_output=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
    )
    return completed.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")


def _git(workspace: Path, *args: str) -> str:
    return _run(workspace, "git", "--no-pager", *args)


def _workspace(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    root = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
    if root != path:
        raise ValueError("Workspace must name the Git worktree root")
    return root


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    Path(handle.name).replace(path)


def workflow_snapshot(
    workspace: str, baseline: str = "", repositories: dict[str, Any] | None = None
) -> str:
    """Hash tracked and nonignored untracked working files, including executable bits."""
    if repositories:
        snapshots = {"primary": json.loads(workflow_snapshot(workspace, baseline))}
        for identifier, assignment in repositories.items():
            snapshots[identifier] = json.loads(
                workflow_snapshot(assignment["workspace"], assignment["baseline"])
            )
        revisions = {key: value["revision"] for key, value in snapshots.items()}
        return json.dumps(
            {
                **{key: value for key, value in snapshots["primary"].items() if key != "files"},
                "revision": hashlib.sha256(
                    json.dumps(revisions, sort_keys=True).encode()
                ).hexdigest(),
                "revisions": revisions,
                "branches": {key: value["branch"] for key, value in snapshots.items()},
                "dirty": any(value["dirty"] for value in snapshots.values()),
                "files": {
                    f"{identifier}::{name}": digest
                    for identifier, snapshot in snapshots.items()
                    for name, digest in snapshot["files"].items()
                },
                "baseline_paths": [
                    f"{identifier}::{name}"
                    for identifier, snapshot in snapshots.items()
                    for name in snapshot.get("baseline_paths", [])
                ],
            }
        )
    root = _workspace(workspace)
    index = _git(root, "ls-files", "--stage")
    if index.startswith("160000 ") or "\n160000 " in index:
        raise ValueError("Submodule worktrees require an explicit verification policy")
    paths = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    digest = hashlib.sha256()
    files = {}

    def update(value: bytes) -> None:
        digest.update(value)
        entry.update(value)

    for name in sorted(set(paths.split("\0")) - {""}):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        mode = path.lstat().st_mode
        entry = hashlib.sha256()

        encoded_name = name.encode("utf-8", errors="surrogateescape")
        update(len(encoded_name).to_bytes(8) + encoded_name)
        if stat.S_ISLNK(mode):
            content = os.fsencode(os.readlink(path))
            update(b"link\0" + len(content).to_bytes(8) + content)
        elif stat.S_ISREG(mode):
            update(b"executable\0" if mode & stat.S_IXUSR else b"file\0")
            update(path.stat().st_size.to_bytes(8))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    update(chunk)
        else:
            raise ValueError(f"Unsupported worktree entry: {name}")
        update(b"\0")
        files[name] = entry.hexdigest()
    scope = {}
    if baseline:
        changed = set(
            _git(root, "diff", "--name-only", "--no-renames", "-z", baseline, "--").split("\0")
        )
        untracked = set(paths.split("\0")) - set(_git(root, "ls-files", "-z").split("\0"))
        scope["baseline_paths"] = sorted((changed | untracked) - {""})
    return json.dumps(
        {
            "revision": digest.hexdigest(),
            "files": files,
            **scope,
            "head": _git(root, "rev-parse", "HEAD"),
            "branch": _git(root, "branch", "--show-current"),
            "dirty": bool(_git(root, "status", "--porcelain")),
        }
    )


def _setup_worktree(path: Path, definition: dict[str, Any]) -> None:
    helper = definition.get("worktree_setup", str(Path.home() / "bin/setup-worktree.py"))
    if helper is False:
        return
    if Path(helper).is_file():
        _run(path, sys.executable, str(Path(helper).resolve()), str(path))
    elif "worktree_setup" in definition:
        raise ValueError(f"Worktree setup helper does not exist: {helper}")


def _worktrees(repository: Path) -> dict[str, Path]:
    entries = _git(repository, "worktree", "list", "--porcelain", "-z")
    result = {}
    for entry in entries.split("\0\0"):
        fields = dict(line.split(" ", 1) for line in entry.split("\0") if " " in line)
        if "branch" in fields:
            result[fields["branch"].removeprefix("refs/heads/")] = Path(fields["worktree"])
    return result


def workflow_review_checkout(repository: str, pull_request_url: str) -> str:
    """Find or create a clean PR checkout without starting review or changing GitHub."""
    if not Path(repository).is_absolute():
        raise ValueError("Repository must be an absolute path")
    root = _workspace(repository)
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?",
        pull_request_url,
    )
    if not match:
        raise ValueError("Use a GitHub pull request URL")
    repo, number = match.groups()
    remote = _git(root, "config", "--get", "remote.origin.url")
    origin = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?", remote
    )
    if not origin or origin[1].lower() != repo.lower():
        raise ValueError("PR repository must match the local origin repository")
    info = json.loads(
        _run(
            root,
            "gh",
            "pr",
            "view",
            number,
            "--repo",
            repo,
            "--json",
            "url,headRefName,headRefOid,baseRefName,baseRefOid,isCrossRepository",
        )
    )
    if info.get("url", "").rstrip("/").lower() != pull_request_url.rstrip("/").lower():
        raise ValueError("GitHub returned a different pull request")
    head, base = info["headRefOid"], info["baseRefOid"]
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, base)
    ):
        raise ValueError("GitHub returned invalid commit IDs")
    branch = f"workflow/review-pr-{number}" if info["isCrossRepository"] else info["headRefName"]
    _git(root, "check-ref-format", "--branch", branch)
    _git(root, "check-ref-format", "refs/heads/" + branch)
    _git(root, "check-ref-format", "refs/heads/" + info["baseRefName"])
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock = common / "omnigent-workflows" / "review-checkout.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock), timeout=0):
        try:
            for sha in (head, base):
                _git(root, "cat-file", "-e", sha + "^{commit}")
        except subprocess.CalledProcessError:
            prefix = f"refs/omnigent/review-pr-{number}"
            _git(
                root,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                f"+refs/pull/{number}/head:{prefix}/head",
                f"+refs/heads/{info['baseRefName']}:{prefix}/base",
            )
            if (
                _git(root, "rev-parse", prefix + "/head") != head
                or _git(root, "rev-parse", prefix + "/base") != base
            ):
                raise ValueError("PR refs changed during fetch; retry preparation") from None
        baseline = _git(root, "merge-base", base, head)
        worktrees = _worktrees(root)
        workspace = worktrees.get(branch, root.parent / f"{root.name}-pr-{number}")
        exists = bool(_git(root, "branch", "--list", branch))
        if exists and _git(root, "rev-parse", "refs/heads/" + branch) != head:
            raise ValueError(
                "Local PR branch differs from GitHub; resolve ownership before review"
            )
        if branch in worktrees:
            if _git(workspace, "status", "--porcelain"):
                raise ValueError(f"PR worktree is dirty; clarify ownership: {workspace}")
        else:
            if workspace.exists() or workspace.is_symlink():
                raise ValueError(f"Unregistered PR worktree path exists: {workspace}")
            if exists:
                _git(root, "worktree", "add", str(workspace), branch)
            else:
                _git(root, "worktree", "add", "-b", branch, str(workspace), head)
        _setup_worktree(workspace, {})
        return json.dumps(
            {
                "repository": str(workspace),
                "branch": branch,
                "baseline": baseline,
                "base": info["baseRefName"],
                "head": head,
                "pull_request_url": info["url"],
            }
        )


def validate_repositories(definition: dict[str, Any]) -> None:
    """Validate explicit secondary repository assignments before preparation."""
    repositories = definition.get("repositories", [])
    if not isinstance(repositories, list):
        raise ValueError("repositories must be a list of assignments")
    ids, paths = {"primary"}, {str(Path(definition["repository"]))}
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) - {
            "id",
            "repository",
            "baseline",
            "base",
            "branch",
            "workspace",
            "remote",
        }:
            raise ValueError("Invalid repository assignment fields")
        for key in ("id", "repository", "baseline", "base"):
            if not isinstance(repository.get(key), str) or not repository[key].strip():
                raise ValueError(f"Repository assignment needs {key}")
        identifier = repository["id"]
        path = str(Path(repository["repository"]))
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or identifier in ids:
            raise ValueError("Repository IDs must be unique kebab-case; primary is reserved")
        if not Path(path).is_absolute() or path in paths:
            raise ValueError("Repositories need distinct absolute paths")
        for key in ("branch", "workspace", "remote"):
            if key in repository and (
                not isinstance(repository[key], str) or not repository[key].strip()
            ):
                raise ValueError(f"Repository {key} must be a nonempty string")
        if "workspace" in repository and not Path(repository["workspace"]).is_absolute():
            raise ValueError("Repository workspace must be absolute")
        if (
            len(definition.get("candidates", [])) != 1
            and {"branch", "workspace"} & repository.keys()
        ):
            raise ValueError("Explicit repository branch/workspace requires one candidate")
        ids.add(identifier)
        paths.add(path)


def repository_definition(
    definition: dict[str, Any], repository: dict[str, Any]
) -> dict[str, Any]:
    """Reuse single-repository preparation for each candidate's secondary checkout."""
    return {
        **{key: value for key, value in definition.items() if key != "repositories"},
        **{
            key: value
            for key, value in repository.items()
            if key not in {"id", "branch", "workspace"}
        },
        "candidates": [
            {
                **{
                    key: value
                    for key, value in candidate.items()
                    if key not in {"branch", "workspace"}
                },
                **{key: repository[key] for key in ("branch", "workspace") if key in repository},
            }
            for candidate in definition["candidates"]
        ],
    }


def workflow_prepare(definition: dict[str, Any]) -> str:
    """Prepare each repository using the same candidate and ownership gates."""
    validate_repositories(definition)
    repositories = definition.get("repositories", [])
    roots = [_workspace(item["repository"]) for item in [definition, *repositories]]
    common_dirs = [
        _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") for root in roots
    ]
    if len(set(common_dirs)) != len(common_dirs):
        raise ValueError("Repository assignments must belong to distinct Git repositories")
    primary = _prepare_repository(
        {key: value for key, value in definition.items() if key != "repositories"}
    )
    result = json.loads(primary)
    for repository in repositories:
        prepared = json.loads(_prepare_repository(repository_definition(definition, repository)))
        for candidate in result["candidates"]:
            assigned = next(
                item for item in prepared["candidates"] if item["id"] == candidate["id"]
            )
            candidate.setdefault("repositories", {})[repository["id"]] = {
                **repository,
                **assigned,
                "id": repository["id"],
                "pr_template": prepared.get("pr_template", ""),
            }
    return json.dumps(result)


def _prepare_repository(definition: dict[str, Any]) -> str:
    """Prepare isolated implementations or bind Review to the existing worktree."""
    repository = _workspace(definition["repository"])
    run_id = definition["run_id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", run_id):
        raise ValueError("run_id must be a lowercase identifier")
    candidates = definition["candidates"]
    review = definition.get("workflow") == "review-fix"
    if review and len(candidates) != 1:
        raise ValueError("Review needs exactly one current implementation")
    ids = [candidate["id"] for candidate in candidates]
    if (
        not ids
        or len(ids) != len(set(ids))
        or any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", item) for item in ids)
    ):
        raise ValueError("Candidates need unique lowercase kebab-case IDs")
    common = Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    path = common / "omnigent-workflows" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    with FileLock(str(path) + ".lock", timeout=0):
        by_branch = _worktrees(repository)
        state: dict[str, Any] | None = json.loads(path.read_text()) if path.exists() else None
        if state is not None and state["definition"] != fingerprint:
            raise ValueError("Run definition changed; use another run ID")
        if state is None:
            if not review and _git(repository, "status", "--porcelain"):
                raise ValueError("Repository has unrelated changes; clarify ownership first")
            baseline = _git(
                repository,
                "rev-parse",
                "--verify",
                f"{definition.get('baseline', 'HEAD')}^{{commit}}",
            )
            state = {
                "definition": fingerprint,
                "repository": str(repository),
                "baseline": baseline,
                "run_id": run_id,
                "task": definition["task"],
                "candidates": [],
            }
            for candidate in candidates:
                if review:
                    branch = _git(repository, "branch", "--show-current")
                    workspace = Path(candidate.get("workspace", repository)).expanduser().resolve()
                    if (
                        not branch
                        or candidate.get("branch", branch) != branch
                        or workspace != repository
                    ):
                        raise ValueError(
                            "Review must use the repository's current branch and worktree"
                        )
                    state["candidates"].append(
                        {
                            **candidate,
                            "workspace": str(repository),
                            "branch": branch,
                            "baseline": baseline,
                        }
                    )
                    continue
                branch = candidate.get("branch", f"workflow/{run_id}/{candidate['id']}")
                _git(repository, "check-ref-format", "--branch", branch)
                workspace = (
                    Path(
                        candidate.get(
                            "workspace",
                            repository.parent / f"{repository.name}-{run_id}-{candidate['id']}",
                        )
                    )
                    .expanduser()
                    .resolve()
                )
                if workspace == repository or any(
                    prior["branch"] == branch or prior["workspace"] == str(workspace)
                    for prior in state["candidates"]
                ):
                    raise ValueError("Candidates require distinct branches and isolated worktrees")
                if _git(repository, "branch", "--list", branch):
                    if (
                        not candidate.get("branch")
                        or not candidate.get("workspace")
                        or branch not in by_branch
                        or by_branch[branch].resolve() != workspace
                    ):
                        raise ValueError(
                            f"Branch {branch} already exists without explicit worktree ownership"
                        )
                    snapshot = json.loads(workflow_snapshot(str(workspace)))
                    if snapshot["dirty"] or snapshot["head"] != baseline:
                        raise ValueError("Existing candidate must be clean at the shared baseline")
                elif workspace.exists():
                    raise ValueError(f"Candidate workspace already exists: {workspace}")
                state["candidates"].append(
                    {
                        **candidate,
                        "workspace": str(workspace),
                        "branch": branch,
                        "baseline": baseline,
                    }
                )
            _save(path, state)
        for candidate in state["candidates"]:
            branch, workspace = candidate["branch"], Path(candidate["workspace"])
            if review:
                if workspace != repository or by_branch.get(branch) != repository:
                    raise ValueError(
                        "Review worktree or branch changed; restore it before resuming"
                    )
                continue
            if branch in by_branch:
                workspace = by_branch[branch].resolve()
                candidate["workspace"] = str(workspace)
            else:
                if workspace.exists():
                    raise ValueError(f"Unregistered candidate path exists: {workspace}")
                if _git(repository, "branch", "--list", branch):
                    _git(repository, "worktree", "add", str(workspace), branch)
                else:
                    _git(
                        repository,
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(workspace),
                        state["baseline"],
                    )
            _setup_worktree(workspace, definition)
        template = Path(state["candidates"][0]["workspace"]) / ".github/pull_request_template.md"
        state["pr_template"] = template.read_text() if template.is_file() else ""
        _save(path, state)
        return json.dumps({key: value for key, value in state.items() if key != "definition"})


def _desktop_environment(workspace: Path) -> dict[str, str]:
    environment = dict(os.environ)
    keys = (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY",
    )
    if any(not environment.get(key) for key in keys):
        session = _run(workspace, "systemctl", "--user", "show-environment")
        for line in session.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in keys and not environment.get(key):
                environment[key] = value
    environment.pop("GPG_TTY", None)
    if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
        raise ValueError("No desktop display is available for graphical commit signing")
    return environment


def validate_publication_text(title: str, body: str, template: str) -> None:
    """Validate generated text before pinning publication parameters."""
    if not title.strip() or "\n" in title or not body.strip():
        raise ValueError("Publication needs a commit subject and complete PR body")
    for heading in re.findall(r"^## .+$", template, re.MULTILINE):
        if heading != "## Changelog" and heading not in body:
            raise ValueError(f"PR body is missing template section: {heading}")
    for row in re.findall(r"^- \[ \] (.+)$", template, re.MULTILINE):
        if f"- [ ] {row}" not in body and f"- [x] {row}" not in body:
            raise ValueError(f"PR body is missing template checkbox: {row}")


def workflow_publish(
    workspace: str,
    revision: str,
    title: str,
    body: str,
    base: str,
    remote: str = "origin",
    repositories: dict[str, Any] | None = None,
    publications: dict[str, dict[str, str]] | None = None,
) -> str:
    """Publish the controller-selected reviewed tree, reconciling each external effect."""
    if repositories:
        snapshot = json.loads(workflow_snapshot(workspace, repositories=repositories))
        if snapshot["revision"] != revision:
            raise ValueError("Reviewed contents changed; rerun reviews and selection")
        if not isinstance(publications, dict) or set(publications) != set(repositories):
            raise ValueError("Publication needs title and body for each secondary repository")
        for identifier, assignment in repositories.items():
            validate_publication_text(
                publications[identifier]["title"],
                publications[identifier]["body"],
                assignment.get("pr_template", ""),
            )
        primary = json.loads(
            workflow_publish(
                workspace, snapshot["revisions"]["primary"], title, body, base, remote
            )
        )
        published = {}
        for identifier, assignment in repositories.items():
            published[identifier] = json.loads(
                workflow_publish(
                    assignment["workspace"],
                    snapshot["revisions"][identifier],
                    title=publications[identifier]["title"],
                    body=publications[identifier]["body"],
                    base=assignment["base"],
                    remote=assignment.get("remote", remote),
                )
            )
        return json.dumps({**primary, "repositories": published})
    root = _workspace(workspace)
    template = root / ".github/pull_request_template.md"
    validate_publication_text(title, body, template.read_text() if template.is_file() else "")
    snapshot = json.loads(workflow_snapshot(str(root)))
    if snapshot["revision"] != revision:
        raise ValueError("Reviewed contents changed; rerun reviews and selection")
    branch = snapshot["branch"]
    if not branch or branch == base or branch in {"main", "master"}:
        raise ValueError("Publication requires a selected feature branch")
    _git(root, "check-ref-format", "--branch", base)
    if remote not in _git(root, "remote").splitlines():
        raise ValueError("Publication remote does not exist")
    push_urls = _git(root, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(push_urls) != 1:
        raise ValueError("Publication requires a single push destination")
    remote_url = push_urls[0]
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?", remote_url
    )
    if match is None:
        raise ValueError("Publication currently requires a GitHub remote")
    repository = match[1]
    gitdir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    path = gitdir / "omnigent-publication.json"
    with _publication_lock(Path(str(path) + ".lock")):
        state = json.loads(path.read_text()) if path.exists() else None
        if (
            state is not None
            and state["revision"] != revision
            and state.get("pr_url")
            and state.get("commit") == snapshot["head"]
            and state.get("tree") == _git(root, "rev-parse", "HEAD^{tree}")
            and all(
                state.get(key) == value
                for key, value in {
                    "base": base,
                    "remote": remote,
                    "push_url": remote_url,
                    "branch": branch,
                }.items()
            )
        ):
            _save(gitdir / f"omnigent-publication.completed-{state['commit']}.json", state)
            state = None
        if (
            state is not None
            and state["revision"] != revision
            and not state.get("commit")
            and snapshot["head"] == state["initial_head"]
        ):
            state = None
        if state is None:
            state = {
                "revision": revision,
                "title": title,
                "body": body,
                "base": base,
                "remote": remote,
                "push_url": remote_url,
                "branch": branch,
                "initial_head": snapshot["head"],
                "commit": None,
            }
        for key, value in {
            "revision": revision,
            "title": title,
            "body": body,
            "base": base,
            "remote": remote,
            "push_url": remote_url,
            "branch": branch,
        }.items():
            if state.get(key) != value:
                raise ValueError(
                    "Publication parameters changed; inspect the existing publication record"
                )
        _save(path, state)
        if state.get("tree") and snapshot["head"] != state["initial_head"]:
            if (
                _git(root, "rev-parse", "HEAD^{tree}") != state["tree"]
                or _git(root, "rev-parse", "HEAD^") != state["initial_head"]
                or _git(root, "log", "-1", "--format=%s") != title
            ):
                raise ValueError("HEAD changed outside the recorded publication")
            state["commit"] = snapshot["head"]
        if not state.get("commit"):
            if not snapshot["dirty"]:
                raise ValueError("No reviewed changes to commit")
            hook = None
            if (root / ".pre-commit-config.yaml").exists():
                local_hook = root / ".venv/bin/pre-commit"
                hook = str(local_hook) if local_hook.is_file() else shutil.which("pre-commit")
                if not hook:
                    raise ValueError(
                        "Install pre-commit in the candidate .venv or on PATH before publication"
                    )
            _git(root, "add", "--all")
            if hook:
                _run(root, hook, "run", "--all-files")
            if json.loads(workflow_snapshot(str(root)))["revision"] != revision:
                raise ValueError(
                    "Pre-commit changed reviewed contents; rerun reviews and selection"
                )
            state["tree"] = _git(root, "write-tree")
            _save(path, state)
            _run(
                root,
                "git",
                "--no-pager",
                "commit",
                "-S",
                "-s",
                "-m",
                title,
                env=_desktop_environment(root),
            )
            state["commit"] = _git(root, "rev-parse", "HEAD")
            _save(path, state)
        _git(root, "verify-commit", state["commit"])
        if _git(root, "rev-parse", "HEAD^{tree}") != state["tree"]:
            raise ValueError("Commit hooks changed the reviewed tree; publication stopped")
        if _git(root, "rev-parse", "HEAD") != state["commit"] or _git(
            root, "status", "--porcelain"
        ):
            raise ValueError("Published commit differs from the selected worktree")
        prs = json.loads(
            _run(
                root,
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                branch,
                "--base",
                base,
                "--state",
                "all",
                "--json",
                "url,headRefOid,state",
            )
        )
        if len(prs) > 1:
            raise ValueError("Several PRs match the selected branch")
        if prs:
            if prs[0]["state"] == "CLOSED":
                raise ValueError("The selected branch's PR was closed; refusing to recreate it")
            if prs[0]["headRefOid"] != state["commit"] and (
                prs[0]["state"] != "OPEN"
                or _git(root, "merge-base", prs[0]["headRefOid"], state["commit"])
                != prs[0]["headRefOid"]
            ):
                raise ValueError("Existing PR cannot fast-forward to the reviewed commit")
            state["pr_url"] = prs[0]["url"]
        if not prs or prs[0]["state"] != "MERGED":
            remote_head = _run(
                root, "gh", "api", f"repos/{repository}/git/matching-refs/heads/{branch}"
            )
            refs = json.loads(remote_head)
            pushed = any(
                ref["ref"] == f"refs/heads/{branch}" and ref["object"]["sha"] == state["commit"]
                for ref in refs
            )
            if not pushed:
                _git(root, "push", remote, f"HEAD:refs/heads/{branch}")
            remote_head = json.loads(
                _run(root, "gh", "api", f"repos/{repository}/git/ref/heads/{branch}")
            )
            if remote_head["object"]["sha"] != state["commit"]:
                raise ValueError("Remote branch does not contain the reviewed commit")
            if prs:
                pr = json.loads(
                    _run(
                        root,
                        "gh",
                        "pr",
                        "view",
                        prs[0]["url"],
                        "--repo",
                        repository,
                        "--json",
                        "headRefOid,state",
                    )
                )
                if pr["headRefOid"] != state["commit"] or pr["state"] == "CLOSED":
                    raise ValueError("Existing PR does not match the reviewed commit")
        if not prs:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as handle:
                handle.write(body)
                handle.flush()
                state["pr_url"] = _run(
                    root,
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--head",
                    branch,
                    "--base",
                    base,
                    "--title",
                    title,
                    "--body-file",
                    handle.name,
                )
        _save(path, state)
        return json.dumps({key: state[key] for key in ("commit", "branch", "pr_url")})
