"""Exercise the extension controller through real server/runner policy events."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _tool(name: str, call_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = (
        arguments
        if arguments is not None
        else (
            {"agent": "worker", "title": "advance", "args": "advance"}
            if name == "sys_session_send"
            else {}
        )
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


def _mock_bundle(destination: Path, child_model: str, mock_base: str) -> Path:
    """Use the shipped bundle, substituting only mock harness configuration."""
    bundle = destination
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples/workflow_controller", bundle)
    config_path = bundle / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["executor"]["config"]["harness"] = "openai-agents"
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
    return bundle


@pytest.mark.timeout(360)
def test_coding_workflow_runs_real_worktree_reviews_and_selected_publication(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
    tmp_path: Path,
) -> None:
    """Real worker tools and snapshots pass all gates; only publication is faked."""
    reset_mock_llm(mock_llm_server_url)
    repository = tmp_path / "repository"
    repository.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "workflow-test@example.invalid"],
        ["config", "user.name", "Workflow Test"],
    ):
        subprocess.run(
            ["/usr/bin/env", "git", *args], cwd=repository, check=True, capture_output=True
        )
    (repository / "greeting.txt").write_text("before\n")
    subprocess.run(["/usr/bin/env", "git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["/usr/bin/env", "git", "-c", "commit.gpgsign=false", "commit", "-m", "baseline"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    suffix = uuid.uuid4().hex[:8]
    parent_model, child_model = f"full-controller-{suffix}", f"full-worker-{suffix}"
    bundle = _mock_bundle(tmp_path / "full-controller", child_model, f"{mock_llm_server_url}/v1")
    publication_record = tmp_path / "publication.json"
    operation_file = bundle / "tools/python/workflow_publish.py"
    source = operation_file.read_text()
    source = (
        "import json\nfrom pathlib import Path\n"
        "from omnigent.workflows.operations import workflow_snapshot as snapshot_files\n" + source
    )
    source = source.replace(
        "return publish(workspace, revision, title, body, base, remote)",
        f"Path({str(publication_record)!r}).write_text(json.dumps(dict(workspace=workspace, "
        "revision=revision, title=title, body=body, base=base, remote=remote)))\n"
        "    snapshot = json.loads(snapshot_files(workspace))\n"
        "    assert snapshot['revision'] == revision\n"
        "    return json.dumps({'commit': 'f' * 40, 'branch': snapshot['branch'], "
        "'pr_url': 'https://example.invalid/pull/1'})",
    )
    operation_file.write_text(source)
    agent = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=bundle,
        name=parent_model,
        model=parent_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent, runner_id=live_runner_id
    )

    def parent_turn(message: str, calls: list[dict[str, Any]], marker: str):
        configure_mock_llm(mock_llm_server_url, [*calls, {"text": marker}], key=parent_model)
        send_user_message_to_session(http_client, session_id=session_id, content=message)
        return _wait_for_text(http_client, session_id, marker)

    def snapshot(call_id: str):
        return _tool("workflow_snapshot", call_id, {"workspace": "/ignored-by-policy"})

    definition = {
        "repository": str(repository),
        "task": "Replace before with hello in greeting.txt.",
        "baseline": "HEAD",
        "base": "main",
        "candidates": [{"id": "a", "task": "Use hello."}],
        "publish": True,
        "worktree_setup": False,
    }
    items = parent_turn(
        "/workflow start " + json.dumps(definition),
        [
            _tool("workflow_prepare", "prepare", {"definition": {}}),
            snapshot("initial-snapshot"),
            _tool("workflow_status", "prepared-status"),
        ],
        "WORKTREE_READY",
    )
    state = _status(items, "prepared-status")
    assert state["status"] == "ready", json.dumps(items)
    candidate = state["candidates"][0]
    workspace = Path(candidate["workspace"])
    assert workspace != repository and (workspace / "greeting.txt").read_text() == "before\n"

    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool("workflow_stage", "question-brief"),
            {
                "text": json.dumps(
                    {
                        "stage": "a/implement",
                        "outcome": "NEEDS_INPUT",
                        "evidence": "The greeting language needs confirmation.",
                        "question": "Which language should the greeting use?",
                    }
                ),
                "block": True,
            },
        ],
        match="Stage: implement\n",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool("sys_session_send", "question-send"),
            {"text": "QUESTION_DISPATCHED"},
            _tool("sys_read_inbox", "question-inbox"),
            _tool("workflow_status", "question-status"),
            {"text": "QUESTION_WAITING"},
        ],
        key=parent_model,
    )
    send_user_message_to_session(
        http_client, session_id=session_id, content="Begin implementation."
    )
    _wait_for_text(http_client, session_id, "QUESTION_DISPATCHED")
    _release_worker(mock_llm_server_url)
    items = _wait_for_text(http_client, session_id, "QUESTION_WAITING")
    assert _status(items, "question-status")["status"] == "waiting-user"
    restart_live_runner()
    items = parent_turn(
        "Show the question again.",
        [_tool("sys_session_send", "paused-send"), _tool("workflow_status", "paused-status")],
        "STILL_WAITING_FOR_ANSWER",
    )
    assert _status(items, "paused-status")["status"] == "waiting-user"
    items = parent_turn(
        "/workflow answer English",
        [snapshot("answer-snapshot"), _tool("workflow_status", "answer-status")],
        "ANSWER_READY",
    )
    assert _status(items, "answer-status")["status"] == "ready"

    groups = (
        ("implement",),
        ("review-a",),
        ("fix-a",),
        ("verify-a",),
        ("review-b-standards", "review-b-spec"),
        ("fix-b",),
        ("verify-b-standards", "verify-b-spec"),
        ("review-c",),
        ("fix-c",),
        ("verify-c",),
        ("final-b-standards", "final-b-spec"),
    )
    stages = [stage for group in groups for stage in group]
    for index, group in enumerate(groups):
        marker = group[0]
        for stage in group:
            report = {
                "stage": f"a/{stage}",
                "outcome": "COMPLETE",
                "evidence": "Read greeting.txt and verified hello.",
                "findings": [],
                "resolved": [],
            }
            child_calls = [_tool("workflow_stage", f"{stage}-brief")]
            if stage == "implement":
                child_calls.append(
                    _tool(
                        "sys_os_write",
                        "outside-write-denied",
                        {"path": str(repository / "greeting.txt"), "content": "escaped\n"},
                    )
                )
                child_calls.append(
                    _tool(
                        "sys_os_write",
                        "implement-write",
                        {"path": str(workspace / "greeting.txt"), "content": "hello\n"},
                    )
                )
            elif stage == "review-a":
                child_calls.append(
                    _tool(
                        "sys_os_write",
                        "review-write-denied",
                        {
                            "path": str(workspace / "greeting.txt"),
                            "content": "must not be written\n",
                        },
                    )
                )
            child_calls.append(
                _tool("sys_os_read", f"{stage}-read", {"path": str(workspace / "greeting.txt")})
            )
            configure_mock_llm(
                mock_llm_server_url,
                [*child_calls, {"text": json.dumps(report), "block": True}],
                match=f"Stage: {stage}\n",
            )
        wake_calls = [_tool("sys_read_inbox", f"{marker}-inbox"), snapshot(f"{marker}-after")]
        if index < len(groups) - 1:
            wake_calls.append(snapshot(f"{marker}-next"))
        halfway = (
            [
                _tool("sys_read_inbox", f"{marker}-first-inbox"),
                _tool("workflow_status", f"{marker}-first-status"),
                {"text": f"HALF_{marker}"},
            ]
            if len(group) == 2
            else []
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                *[_tool("sys_session_send", f"{stage}-send") for stage in group],
                {"text": f"DISPATCHED_{marker}"},
                *halfway,
                *wake_calls,
                _tool("workflow_status", f"{marker}-status"),
                {"text": f"FINISHED_{marker}"},
            ],
            key=parent_model,
        )
        send_user_message_to_session(
            http_client, session_id=session_id, content=f"Continue {marker}."
        )
        _wait_for_text(http_client, session_id, f"DISPATCHED_{marker}")
        _release_worker(mock_llm_server_url)
        if len(group) == 2:
            items = _wait_for_text(http_client, session_id, f"HALF_{marker}")
            halfway_state = _status(items, f"{marker}-first-status")
            assert halfway_state["status"] == "running", halfway_state
            assert not any(f"a/{stage}" in halfway_state["completed"] for stage in group)
            _release_worker(mock_llm_server_url)
        items = _wait_for_text(http_client, session_id, f"FINISHED_{marker}")
        state = _status(items, f"{marker}-status")
        for stage in group:
            assert f"a/{stage}" in state["completed"], state
            child_id = _status(items, f"{stage}-send")["conversation_id"]
            child_items = http_client.get(
                f"/v1/sessions/{child_id}/items", params={"limit": 1000}
            ).json()["data"]
            read = next(
                item["output"]
                for item in child_items
                if item.get("call_id") == f"{stage}-read"
                and item.get("type") == "function_call_output"
            )
            assert "hello" in read and "error" not in read.lower(), read
            assert (workspace / "greeting.txt").read_text() == "hello\n", child_items
            if stage == "implement":
                outside = next(
                    item["output"]
                    for item in child_items
                    if item.get("call_id") == "outside-write-denied"
                    and item.get("type") == "function_call_output"
                )
                assert "denied" in outside.lower(), outside
            if stage == "review-a":
                denied = [
                    item
                    for item in child_items
                    if item.get("call_id") == "review-write-denied"
                    and item.get("type") == "function_call_output"
                ]
                assert denied and "denied" in str(denied[0]["output"]).lower(), child_items
                restart_live_runner()
        assert (repository / "greeting.txt").read_text() == "before\n"
        assert not publication_record.exists()

    assert state["status"] == "waiting-user" and state["selected"] is None
    assert set(state["completed"]) == {f"a/{stage}" for stage in stages}
    restart_live_runner()
    publish_args = {
        "workspace": "/ignored-by-policy",
        "revision": "ignored",
        "title": "test: greet user",
        "body": "Replace the greeting after all review checks passed.",
        "base": "ignored",
    }
    items = parent_turn(
        "Show the result before selection.",
        [
            _tool("workflow_publish", "unselected-publish", publish_args),
            _tool("workflow_status", "unselected-status"),
        ],
        "SELECTION_STILL_REQUIRED",
    )
    assert _status(items, "unselected-status")["selected"] is None
    assert not publication_record.exists()
    items = parent_turn(
        "/workflow select a",
        [
            snapshot("selection-snapshot"),
            _tool("workflow_publish", "selected-publish", publish_args),
            _tool("workflow_status", "published-status"),
        ],
        "WORKFLOW_PUBLISHED",
    )
    complete = _status(items, "published-status")
    assert complete["status"] == "complete", complete
    assert complete["publication"]["pr_url"] == "https://example.invalid/pull/1"
    published = json.loads(publication_record.read_text())
    assert published["workspace"] == str(workspace)
    assert published["revision"] == complete["verified"]["a"]
    assert published["base"] == "main"
