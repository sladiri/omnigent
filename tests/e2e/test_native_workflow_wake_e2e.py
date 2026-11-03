"""Native workflow workers wake their controller and surface a required decision."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.workflows.operations import _git
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    release_mock_gate,
    upload_agent,
)
from tests.e2e.test_codex_subagent_session_budget_e2e import (
    _CHILD_MODEL,
    _PARENT_MODEL,
    _snapshot,
    _wait_for,
)
from tests.e2e.test_codex_subagent_session_budget_e2e import (
    codex_budget_rig as codex_budget_rig,
)
from tests.e2e.test_workflow_controller_e2e import _wait_for_text


def _native_tool(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    return {
        "native_items": [
            {
                "type": "function_call",
                "id": "fc-" + call_id,
                "call_id": call_id,
                "namespace": "mcp__omnigent",
                "name": name,
                "arguments": json.dumps(args),
            }
        ]
    }


@pytest.mark.timeout(240)
def test_native_controller_surfaces_worker_question_without_user_message(
    codex_budget_rig: tuple[httpx.Client, Path, str, str],
    tmp_path: Path,
) -> None:
    client, repository, runner_id, mock_url = codex_budget_rig
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Workflow Test")
    _git(repository, "config", "user.email", "workflow@example.invalid")
    (repository / "source.txt").write_text("unchanged\n")
    _git(repository, "add", "source.txt")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    bundle = tmp_path / "workflow-bundle"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples/workflow_controller", bundle)
    for path, model in (
        (bundle / "config.yaml", _PARENT_MODEL),
        (bundle / "agents/worker/config.yaml", _CHILD_MODEL),
    ):
        spec = yaml.safe_load(path.read_text())
        spec["executor"]["auth"] = {"type": "provider", "name": "budget-test"}
        spec["executor"]["config"]["yolo"] = True
        spec["llm"] = {"model": model}
        path.write_text(yaml.safe_dump(spec))
    for marker in ("<session>", "<user_message>", "Generate a concise, single-line task title"):
        configure_mock_llm(mock_url, [{"text": "Workflow test"}], match=marker)
    question = "May I use a read-only Registry batch query?"
    configure_mock_llm(
        mock_url,
        [
            _native_tool("workflow_prepare", "prepare", definition={}),
            _native_tool("workflow_snapshot", "before", workspace=str(repository)),
            _native_tool(
                "sys_session_send", "dispatch", agent="worker", title="advance", args="advance"
            ),
            {"text": "WORKER_DISPATCHED_WAITING"},
            _native_tool("sys_read_inbox", "inbox"),
            _native_tool("workflow_status", "waiting-status"),
            {"text": question},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(
        mock_url,
        [
            _native_tool("workflow_stage", "brief"),
            {
                "text": json.dumps(
                    {
                        "stage": "implementation/implement",
                        "outcome": "NEEDS_INPUT",
                        "evidence": "Existing integration does not prove deletion.",
                        "question": question,
                        "findings": [],
                        "resolved": [],
                    }
                ),
                "block": True,
            },
        ],
        key=_CHILD_MODEL,
    )
    name = upload_agent(client, bundle)
    sid = create_runner_bound_session(client, agent_name=name, runner_id=runner_id)
    definition = {
        "workflow": "implement-review-fix",
        "repository": str(repository),
        "task": "Inspect the source, then ask before implementing.",
        "baseline": "HEAD",
        "base": "main",
        "publish": False,
        "candidates": [{"id": "implementation", "task": "Inspect the source"}],
    }
    response = client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "/workflow start " + json.dumps(definition)}
                ],
            },
        },
    )
    response.raise_for_status()
    assert response.json().get("queued") is True, response.text
    _wait_for_text(client, sid, "WORKER_DISPATCHED_WAITING")
    _wait_for(
        lambda: _snapshot(client, sid).get("status") == "idle",
        description="parent idle",
        deadline=time.monotonic() + 30,
    )
    with httpx.Client(base_url=mock_url, trust_env=False) as mock:
        _wait_for(
            lambda: mock.get("/gate/pending").json()["pending"],
            description="worker report gate",
            deadline=time.monotonic() + 30,
        )
    release_mock_gate(mock_url)

    def displayed_question() -> list[dict[str, Any]] | None:
        response = client.get(f"/v1/sessions/{sid}/items", params={"limit": 1000})
        response.raise_for_status()
        items = response.json()["data"]
        if any(
            item.get("role") == "assistant"
            and any(part.get("text") == question for part in item.get("content", []))
            for item in items
        ):
            return items
        return None

    items = _wait_for(
        displayed_question,
        description="question displayed by the controller",
        deadline=time.monotonic() + 40,
    )
    response = client.post(
        f"/v1/sessions/{sid}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_RESULT",
                "target": "",
                "context": {},
                "data": {"result": "stub"},
                "request_data": {"name": "workflow_status"},
            }
        },
    )
    response.raise_for_status()
    state = json.loads(response.json()["data"])
    assert state["status"] == "waiting-user"
    assert state["question"] == question
    assert state["completed"] == {}
    assert (repository / "source.txt").read_text() == "unchanged\n"
    assert "waiting in inbox" in json.dumps(items)
