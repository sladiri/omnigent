"""Host snapshot with arguments supplied by the workflow policy."""

from omnigent_client.tools import tool

from omnigent.workflows.operations import workflow_snapshot as snapshot


@tool
def workflow_snapshot(workspace: str, baseline: str = "") -> str:
    """Read a revision of all tracked and nonignored working files."""
    return snapshot(workspace, baseline)
