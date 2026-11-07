"""Access the existing repository journal without changing workflow state."""

from omnigent_client.tools import tool

from omnigent.workflows.journal import workflow_journal as journal


@tool
def workflow_journal(repository: str, action: str, issue: str = "", text: str = "") -> str:
    """Journal in Beads; policy supplies the repository, so pass repository="".

    actions: instructions (resolve journal and read PRIME.md), list (session journals),
    show, create (text="Session: topic (date)"), claim, comment (text), close (text=reason).
    Use the existing session journal when present. Writes require repository instructions
    or a user request. Only session,agent-journal labeled issues may be changed.
    No initialization, deletion, shell execution, or workflow approval is supported.
    """
    return journal(repository, action, issue, text)
