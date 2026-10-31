"""Read the assigned stage brief supplied by the controller policy."""

from omnigent_client.tools import tool


@tool
def workflow_stage() -> str:
    """Read this worker's task, worktree, checks, prior evidence, and report contract."""
    return "Workflow policy is not installed."
