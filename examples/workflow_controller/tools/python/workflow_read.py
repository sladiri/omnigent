"""Read the original repository, including context outside the session subfolder."""

from omnigent_client.tools import tool

from omnigent.workflows.repository import workflow_read as read


@tool
def workflow_read(repository: str, path: str = "", offset: int = 1, limit: int = 2000) -> str:
    """Read files or list directories anywhere in the original repository.

    Pass repository=""; the controller supplies the trusted source repository.
    Paths may be absolute or relative to its Git root. Empty path lists the root.
    Offset is 1-based; limit is at most 2000 lines or directory entries.
    Use for shared instructions and code outside the assigned candidate.
    This tool cannot write, run commands, or approve workflow transitions.
    """
    return read(repository, path, offset, limit)
