"""Read source-repository context without granting worker write access."""

import json
from pathlib import Path

from omnigent.host.git_worktree import list_worktrees
from omnigent.inner.os_env import _read_impl
from omnigent.workflows.operations import _git


def workflow_read(repository: str, path: str = "", offset: int = 1, limit: int = 2000) -> str:
    """Read source context from the selected checkout or its main checkout."""
    if not Path(repository).is_absolute():
        raise ValueError("Repository must be absolute")
    root = Path(
        _git(Path(repository).resolve(strict=True), "rev-parse", "--show-toplevel")
    ).resolve()
    target = (root / path).resolve(strict=True)
    if not target.is_relative_to(root):
        main = Path(list_worktrees(repo_path=str(root))[0].path).resolve(strict=True)
        if not target.is_relative_to(main):
            raise ValueError("Read must stay inside the source repository or its main checkout")
    if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= 2000:
        raise ValueError("Use a positive offset and a limit from 1 to 2000")
    if target.is_dir():
        entries = sorted(item.name for item in target.iterdir())
        result = {
            "entries": entries[offset - 1 : offset - 1 + limit],
            "total_entries": len(entries),
        }
    elif target.is_file():
        result = _read_impl(target, offset, limit)
    else:
        raise ValueError("Read requires a regular file or directory")
    return json.dumps({"repository": str(root), "path": str(target), **result})
