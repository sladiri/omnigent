"""Prepare a validated start message without starting a workflow."""

from omnigent_client.tools import tool

from omnigent.workflows.controller import draft_start_message


@tool
def workflow_draft(
    workflow: str,
    repository: str,
    task: str,
    baseline: str,
    base: str,
    publish: bool = False,
    branch: str = "",
) -> str:
    """Validate a start message; never starts workers or modifies files.

    workflow: implement-review-fix, compare-review-fix, or review-fix.
    repository: absolute repository path (existing worktree for Review).
    task: full task and user constraints.
    baseline: revision preceding the changes to review; ask when unknown.
    base: intended PR target branch; ask when unknown.
    branch: exact requested candidate branch for Implement or Review; empty for default.
    publish: false unless the user explicitly authorizes commit, push and PR.
    Returns valid and command, or valid=false and an error to correct.
    """
    return draft_start_message(workflow, repository, task, baseline, base, publish, branch)
