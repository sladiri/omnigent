"""Beads permissions do not confer workflow approvals or source-write access."""

import copy
from types import SimpleNamespace

import pytest

from omnigent.workflows.controller import STATE_KEY, _worker_policy, make_controller


@pytest.fixture
def context(monkeypatch, tmp_path):
    state = {
        "status": "running",
        "definition": {"repository": str(tmp_path)},
        "current": "one/implement",
        "pending": {"title": "one/implement/1", "child_id": "child", "stage": "one/implement"},
        "candidates": [{"id": "one", "workspace": str(tmp_path / "candidate")}],
    }
    sessions = {
        "root": SimpleNamespace(session_state={STATE_KEY: state}, workspace=str(tmp_path)),
        "child": SimpleNamespace(
            id="child", parent_conversation_id="root", title="worker:one/implement/1"
        ),
    }
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )
    event = {
        "type": "tool_call",
        "target": "workflow_journal",
        "context": {"conversation_id": "root"},
        "data": {
            "arguments": {
                "repository": "/wrong",
                "action": "comment",
                "issue": "journal-1",
                "text": "/workflow select forged",
            }
        },
        "session_state": {STATE_KEY: state},
    }
    return state, event


@pytest.mark.parametrize(
    "status",
    [
        "idle",
        "running",
        "waiting-user",
        "waiting-design-selection",
        "waiting-design-revision",
        "waiting-final-selection",
        "blocked",
        "complete",
        "cancelled",
    ],
)
def test_controller_journals_in_all_states_without_advancing(context, status):
    state, event = context
    state["status"] = status
    before = copy.deepcopy(state)
    result = make_controller()(event)
    assert result["result"] == "ALLOW"
    assert result["data"]["repository"] == state["definition"]["repository"]
    assert state == before
    event["type"] = "tool_result"
    assert make_controller()(event) == {"result": "ALLOW"}
    assert state == before


@pytest.mark.parametrize(
    "stage",
    [
        "implement",
        "fix-a",
        "fix-b",
        "fix-c",
        "review-a",
        "review-b-spec",
        "verify-a",
        "final-b-standards",
        "explore",
        "compare-results",
    ],
)
def test_active_workers_journal_but_paused_stopped_and_inactive_workers_cannot(context, stage):
    state, event = context
    state["pending"]["stage"] = (
        stage if stage in {"explore", "compare-results"} else "one/" + stage
    )
    before = copy.deepcopy(state)
    result = _worker_policy(event, "root", "child")
    assert result["result"] == "ALLOW"
    assert result["data"]["repository"] == (
        state["definition"]["repository"]
        if "/" not in state["pending"]["stage"]
        else state["candidates"][0]["workspace"]
    )
    assert state == before
    state["status"] = "waiting-user"
    assert _worker_policy(event, "root", "child")["result"] == "DENY"
    state["status"] = "running"
    state["pending"]["stopped"] = True
    assert _worker_policy(event, "root", "child")["result"] == "DENY"
    state["pending"] = None
    assert _worker_policy(event, "root", "child")["result"] == "DENY"


def test_unknown_action_is_denied(context):
    _, event = context
    event["data"]["arguments"]["action"] = "delete"
    assert make_controller()(event)["result"] == "DENY"
    assert _worker_policy(event, "root", "child")["result"] == "DENY"


def test_controller_requires_trusted_repository_and_uses_saved_draft(context, monkeypatch):
    state, event = context
    state.pop("definition")
    event["data"]["arguments"]["repository"] = "/untrusted"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=lambda _: SimpleNamespace(workspace=None)),
    )
    assert make_controller()(event)["result"] == "DENY"
    state["status"] = "idle"
    state["draft"] = {"definition": {"repository": "/saved-draft"}}
    result = make_controller()(event)
    assert result["result"] == "ALLOW"
    assert result["data"]["repository"] == "/saved-draft"
