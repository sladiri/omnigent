"""Prepare a PR implementation checkout before requesting workflow start approval."""

from omnigent_client.tools import tool

from omnigent.workflows.operations import workflow_review_checkout as checkout


@tool
def workflow_review_checkout(repository: str, pull_request_url: str) -> str:
    """Read PR metadata and find or create its clean worktree, without starting review.

    repository: absolute local Git repository root with the PR repository as origin.
    pull_request_url: https://github.com/owner/repo/pull/number.
    Returns repository, branch, Git baseline and merge target base for workflow_draft.
    Preserves existing work; conflicting branches or dirty worktrees need a user decision.
    Fetch may wait for the user's hardware key. Never commits, pushes or changes GitHub.
    """
    return checkout(repository, pull_request_url)
