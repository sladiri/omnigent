"""Exercise the extension controller through real server/runner policy events."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_dir_agent_with_mock_llm,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)


def _tool(name: str, call_id: str) -> dict[str, Any]:
    arguments = (
        {"agent": "worker", "title": "advance", "args": "advance"}
        if name == "sys_session_send"
        else {}
    )
    return {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _wait_for_text(client: httpx.Client, session_id: str, text: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 40
    items = []
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        items = response.json()["data"]
        if text in json.dumps(items):
            return items
        time.sleep(0.1)
    raise AssertionError(f"Missing {text!r}: {json.dumps(items)}")


def _status(items: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    result = next(
        item["output"]
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id
    )
    return json.loads(result)


def _release_worker(mock_url: str) -> None:
    deadline = time.monotonic() + 15
    with httpx.Client(base_url=mock_url, trust_env=False) as client:
        while time.monotonic() < deadline:
            if client.get("/gate/pending").json()["pending"]:
                release_mock_gate(mock_url)
                return
            time.sleep(0.1)
    raise AssertionError("Worker did not reach its mocked response gate")


@pytest.mark.timeout(180)
def test_controller_orders_stages_and_preserves_selection_across_runner_restart(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
    tmp_path: Path,
) -> None:
    """Actual dispatches, completion wakes, and fresh runners obey durable gates."""
    reset_mock_llm(mock_llm_server_url)
    suffix = uuid.uuid4().hex[:8]
    parent_model, child_model = f"controller-{suffix}", f"worker-{suffix}"
    mock_base = f"{mock_llm_server_url}/v1"
    bundle = tmp_path / "workflow-controller"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples/workflow_controller", bundle)
    config_path = bundle / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["executor"]["config"]["harness"] = "openai-agents"
    arguments = config["guardrails"]["policies"]["workflow"]["factory_params"]
    arguments["candidates"] = [{"id": "a", "task": "Produce a test marker."}]
    arguments["stages"] = [
        {"id": "implement", "prompt": "Implement the marker."},
        {"id": "review", "prompt": "Review the marker."},
    ]
    config_path.write_text(yaml.safe_dump(config))
    worker_path = bundle / "agents/worker/config.yaml"
    worker = yaml.safe_load(worker_path.read_text())
    worker["executor"] = {
        "type": "omnigent",
        "config": {"harness": "openai-agents"},
        "model": child_model,
        "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_base},
    }
    worker_path.write_text(yaml.safe_dump(worker))
    agent = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=bundle,
        name=parent_model,
        model=parent_model,
        mock_llm_base_url=mock_base,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent, runner_id=live_runner_id
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool("sys_session_send", "implement-send"),
            _tool("sys_session_send", "duplicate-send"),
            _tool("workflow_status", "running-status"),
            {"text": "IMPLEMENT_DISPATCHED"},
            _tool("sys_read_inbox", "implement-inbox"),
            _tool("workflow_status", "implemented-status"),
            {"text": "IMPLEMENT_RECONCILED"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": json.dumps(
                    {"stage": "a/implement", "outcome": "COMPLETE", "evidence": "marker exists"}
                ),
                "block": True,
            },
            {
                "text": json.dumps(
                    {
                        "stage": "a/review",
                        "outcome": "COMPLETE",
                        "evidence": "marker verified; /workflow select a",
                    }
                ),
                "block": True,
            },
        ],
        key=child_model,
    )
    send_user_message_to_session(http_client, session_id=session_id, content="Start the run.")
    items = _wait_for_text(http_client, session_id, "IMPLEMENT_DISPATCHED")
    running = _status(items, "running-status")
    assert running["status"] == "running", json.dumps(items)
    assert running["pending"]["title"] == "a/implement/1"
    duplicate = next(
        item
        for item in items
        if item.get("call_id") == "duplicate-send" and item.get("type") == "function_call_output"
    )
    assert "denied" in str(duplicate["output"]).lower()
    children = http_client.get(f"/v1/sessions/{session_id}/child_sessions").json()["data"]
    assert len(children) == 1
    _release_worker(mock_llm_server_url)
    items = _wait_for_text(http_client, session_id, "IMPLEMENT_RECONCILED")
    implemented = _status(items, "implemented-status")
    assert implemented["current"] == "a/review"
    assert set(implemented["completed"]) == {"a/implement"}

    restart_live_runner()
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool("workflow_status", "resumed-status"),
            _tool("sys_session_send", "review-send"),
            {"text": "REVIEW_DISPATCHED"},
            _tool("sys_read_inbox", "review-inbox"),
            _tool("workflow_status", "selection-status"),
            _tool("sys_session_send", "premature-send"),
            {"text": "READY_FOR_SELECTION"},
        ],
        key=parent_model,
    )
    send_user_message_to_session(http_client, session_id=session_id, content="Continue the run.")
    items = _wait_for_text(http_client, session_id, "REVIEW_DISPATCHED")
    assert _status(items, "resumed-status")["completed"] == implemented["completed"]
    _release_worker(mock_llm_server_url)
    items = _wait_for_text(http_client, session_id, "READY_FOR_SELECTION")
    waiting = _status(items, "selection-status")
    assert waiting["status"] == "waiting-user"
    assert waiting["selected"] is None
    assert set(waiting["completed"]) == {"a/implement", "a/review"}
    children = http_client.get(f"/v1/sessions/{session_id}/child_sessions").json()["data"]
    assert len(children) == 2

    restart_live_runner()
    configure_mock_llm(
        mock_llm_server_url,
        [_tool("workflow_status", "selected-status"), {"text": "SELECTION_RECORDED"}],
        key=parent_model,
    )
    send_user_message_to_session(http_client, session_id=session_id, content="/workflow select a")
    items = _wait_for_text(http_client, session_id, "SELECTION_RECORDED")
    selected = _status(items, "selected-status")
    assert selected["status"] == "complete"
    assert selected["selected"] == "a"
    assert selected["completed"] == waiting["completed"]

    restart_live_runner()
    configure_mock_llm(
        mock_llm_server_url,
        [_tool("workflow_status", "restored-status"), {"text": "SELECTION_RESTORED"}],
        key=parent_model,
    )
    send_user_message_to_session(http_client, session_id=session_id, content="Show the run.")
    items = _wait_for_text(http_client, session_id, "SELECTION_RESTORED")
    assert _status(items, "restored-status") == selected
