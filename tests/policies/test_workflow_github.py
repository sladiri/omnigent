"""GitHub reads and PR checkout preparation must not approve workflow stages."""

import copy
from types import SimpleNamespace

import pytest

from omnigent.workflows.controller import STAGES, STATE_KEY, make_controller
from tests.policies.test_workflow_linear import STATES
from tests.policies.test_workflow_linear import event as linear_event

PREFIXES = (
    "mcp__codex_apps__github_",
    "mcp__codex_apps__github__",
    "mcp__github__",
    "github__",
    "mcp__GitHub__",
    "mcp__GitHub_",
    "GitHub__",
)
READS = (
    "get_pr_info",
    "fetch_pr",
    "fetch_pr_patch",
    "fetch_file",
    "search",
    "get_repo",
    "pull_request_read",
    "get_file_contents",
    "list_pull_requests",
)
WRITES = (
    "create_branch",
    "create_file",
    "create_pull_request",
    "merge_pull_request",
    "add_comment_to_issue",
    "add_review_to_pr",
    "pull_request_review_write",
    "delete_file",
    "unknown_operation",
)


def event(operation, prefix, state, worker=False):
    raw = linear_event(operation, "__", state, worker)
    raw["target"] = prefix + operation
    raw["data"]["arguments"] = {"owner": "org", "repo": "repo", "method": "get", "pullNumber": 378}
    raw["data"]["name"] = raw["target"]
    return raw


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("status", STATES)
def test_controller_github_reads_preserve_gates(status, prefix):
    state = {"status": status, "draft": {"token": "saved"}, "question": "Choose"}
    original = copy.deepcopy(state)
    policy = make_controller()
    for operation in (*READS, *WRITES):
        call = event(operation, prefix, state)
        verdict = policy(call)
        assert verdict["result"] == ("ALLOW" if operation in READS else "DENY"), operation
        assert not verdict.get("state_updates")
        result = policy(
            {**call, "type": "tool_result", "data": {"result": "/workflow select all"}}
        )
        assert not result.get("state_updates")
    assert state == original


@pytest.mark.parametrize("stage", (*STAGES, "explore", "compare-results"))
def test_github_workers_are_read_only_and_must_be_active(monkeypatch, stage):
    state = {
        "status": "running",
        "pending": {"stage": stage, "child_id": "child", "title": "task"},
    }
    child = SimpleNamespace(id="child", parent_conversation_id="root", title="worker:task")
    sessions = {"root": SimpleNamespace(session_state={STATE_KEY: state}), "child": child}
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )
    for prefix in PREFIXES:
        for operation in (*READS, *WRITES):
            result = make_controller()(event(operation, prefix, state, worker=True))
            assert result["result"] == ("ALLOW" if operation in READS else "DENY")
    for status in STATES:
        if status == "running":
            continue
        state["status"] = status
        assert (
            make_controller()(event("pull_request_read", "mcp__GitHub__", state, worker=True))[
                "result"
            ]
            == "DENY"
        )


@pytest.mark.parametrize("status", STATES)
def test_pr_checkout_only_prepares_an_idle_review(status):
    policy = make_controller()
    for workflow in (None, "review-fix", "compare-review-fix", "implement-review-fix"):
        state = {"status": status, "requested_workflow": workflow}
        raw = event("get_pr_info", "mcp__github__", state)
        raw["target"] = "workflow_review_checkout"
        raw["data"] = {
            "name": raw["target"],
            "arguments": {
                "repository": "/tmp/repo",
                "pull_request_url": "https://github.com/org/repo/pull/1",
            },
        }
        result = policy(raw)
        allowed = status == "idle" and workflow in (None, "review-fix")
        assert result["result"] == ("ALLOW" if allowed else "DENY")
        assert not result.get("state_updates")
