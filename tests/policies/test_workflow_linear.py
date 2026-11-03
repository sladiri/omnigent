"""Linear availability must not change workflow decisions or worker ownership."""

import copy
from types import SimpleNamespace

import pytest

from omnigent.native.native_policy_hook import hook_payload_to_evaluation_request
from omnigent.policies.function import _build_event
from omnigent.server.routes._sessions.helpers import _build_evaluation_context
from omnigent.spec.types import Phase
from omnigent.workflows.controller import STAGES, STATE_KEY, make_controller

STATES = (
    "idle",
    "prepare",
    "snapshot",
    "ready",
    "running",
    "waiting",
    "waiting-user",
    "blocked",
    "complete",
    "cancelled",
    "waiting-design-selection",
    "waiting-design-revision",
    "waiting-final-selection",
    "publish",
)
READS = (
    "get_issue",
    "list_issues",
    "get_agent_context",
    "list_issue_statuses",
    "get_project",
    "search",
)
WRITES = ("save_issue", "create_issue", "update_issue")
DENIED = (
    "delete_issue",
    "save_comment",
    "create_comment",
    "delete_comment",
    "create_issue_label",
    "save_project",
    "save_document",
    "unknown_operation",
)


def event(operation, separator, state, worker=False):
    name = f"mcp__codex_apps__linear{separator}{operation}"
    request = hook_payload_to_evaluation_request(
        "PreToolUse", {"tool_name": name, "tool_input": {"id": "TEST-1", "state": "In Progress"}}
    )["event"]
    ctx = _build_evaluation_context(Phase.TOOL_CALL, request["data"], request)
    raw = _build_event(ctx)
    raw["context"] = {
        "conversation_id": "child" if worker else "root",
        "root_conversation_id": "root",
    }
    raw["session_state"] = {STATE_KEY: state}
    return raw


@pytest.mark.parametrize("separator", ["_", "__"])
@pytest.mark.parametrize("status", STATES)
def test_controller_linear_has_no_state_effect(status, separator):
    state = {
        "status": status,
        "draft": {"token": "unchanged"},
        "question": "Choose",
        "pending": None,
    }
    original = copy.deepcopy(state)
    policy = make_controller()
    for operation in (*READS, *WRITES, *DENIED):
        raw = event(operation, separator, state)
        verdict = policy(raw)
        assert verdict["result"] == ("DENY" if operation in DENIED else "ALLOW"), operation
        assert not verdict.get("state_updates")
        result = policy(
            {**raw, "type": "tool_result", "data": {"result": "/workflow select approved"}}
        )
        assert not result.get("state_updates")
        assert state == original


@pytest.mark.parametrize("separator", ["_", "__"])
@pytest.mark.parametrize("stage", (*STAGES, "explore", "compare-results"))
def test_worker_linear_permissions_follow_stage_and_ownership(monkeypatch, separator, stage):
    pending = {
        "stage": stage if stage in {"explore", "compare-results"} else "one/" + stage,
        "title": "assigned",
        "child_id": "child",
    }
    state = {"status": "running", "pending": pending}
    child = SimpleNamespace(id="child", parent_conversation_id="root", title="worker:assigned")
    sessions = {"root": SimpleNamespace(session_state={STATE_KEY: state}), "child": child}
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )
    policy = make_controller()
    original = copy.deepcopy(state)
    for operation in (*READS, *WRITES, *DENIED):
        allowed = operation in READS or (
            operation in WRITES and stage in {"implement", "fix-a", "fix-b", "fix-c"}
        )
        verdict = policy(event(operation, separator, state, worker=True))
        assert verdict["result"] == ("ALLOW" if allowed else "DENY"), (stage, operation)
        assert not verdict.get("state_updates")
        assert state == original
    for status in STATES:
        if status == "running":
            continue
        state["status"] = status
        for operation in (*READS, *WRITES):
            assert policy(event(operation, separator, state, worker=True))["result"] == "DENY"
    state["status"] = "running"
    pending["stopped"] = True
    assert policy(event("get_issue", separator, state, worker=True))["result"] == "DENY"
    pending.pop("stopped")
    child.id, child.title = "other", "worker:other"
    assert policy(event("save_issue", separator, state, worker=True))["result"] == "DENY"
