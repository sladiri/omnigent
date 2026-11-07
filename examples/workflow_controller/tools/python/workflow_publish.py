"""Host publication with arguments supplied by the workflow policy."""

from omnigent_client.tools import tool

from omnigent.workflows.operations import workflow_publish as publish


@tool
def workflow_publish(
    workspace: str, revision: str, title: str, body: str, base: str, remote: str = "origin"
) -> str:
    """Publish only the controller-selected reviewed candidate."""
    return publish(workspace, revision, title, body, base, remote)
