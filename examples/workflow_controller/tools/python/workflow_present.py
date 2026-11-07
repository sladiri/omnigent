"""Present the controller's persisted proposal or result comparison."""

from omnigent_client.tools import tool


@tool
def workflow_present() -> str:
    """Show the current comparison and exact user decision commands."""
    return "Workflow policy is not installed."
