"""Host preparation with arguments supplied by the workflow policy."""

from omnigent_client.tools import tool

from omnigent.workflows.operations import workflow_prepare as prepare


@tool
def workflow_prepare(definition: dict) -> str:
    """Prepare isolated candidate worktrees from a fixed run definition."""
    return prepare(definition)
