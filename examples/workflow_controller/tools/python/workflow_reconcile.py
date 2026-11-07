"""Request recovery through the server-side workflow policy."""

from omnigent_client.tools import tool


@tool
def workflow_reconcile() -> str:
    """Recover an uncertain dispatch from stored child sessions and completed work."""
    return "Workflow policy is not installed."
