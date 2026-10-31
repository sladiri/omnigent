"""Read the workflow snapshot supplied by the server-side policy."""

from omnigent_client.tools import tool


@tool
def workflow_status() -> str:
    """Read durable workflow state and the next allowed action."""
    return "Workflow policy is not installed."
