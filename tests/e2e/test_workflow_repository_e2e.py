"""A real isolated runner reads the source checkout and persists its user wait."""

import json
from pathlib import Path

import httpx
import pytest

from omnigent.workflows.operations import _git
from tests._helpers.operator_agents import operator_agent_store
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_dir_agent_with_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.test_workflow_controller_e2e import (
    _mock_bundle,
    _release_worker,
    _status,
    _tool,
    _wait_for_text,
)
from tests.tools.test_workflow_operations import repository as repository


@pytest.mark.parametrize("review_existing", [False, True])
@pytest.mark.timeout(150)
def test_worker_reads_original_checkout_and_keeps_wait_after_restart(
    repository, isolated_mock_llm_server_url, tmp_path, review_existing
):
    mock_url = isolated_mock_llm_server_url
    model = "mock-repository-controller"
    bundle = _mock_bundle(tmp_path / "bundle", "mock-repository-worker", f"{mock_url}/v1")
    (repository / ".git/info/exclude").write_text(".codex/\nCLAUDE.local.md\n")
    (repository / ".codex").mkdir()
    (repository / ".codex/config.toml").write_text('developer_instructions = "Shared guidance"')
    (repository / "CLAUDE.local.md").write_text("Original checkout instructions")
    source = repository
    stage = "implement"
    if review_existing:
        source = tmp_path / "candidate"
        _git(repository, "worktree", "add", "-b", "candidate", str(source))
        stage = "review-a"
    with (
        server_runner(tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2]) as stack,
        operator_agent_store(stack.base_url, stack.database_uri, stack.artifact_location),
    ):
        stack.start_runner()
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            name = register_dir_agent_with_mock_llm(
                client,
                agent_dir=bundle,
                name="repository-read",
                model=model,
                mock_llm_base_url=f"{mock_url}/v1",
            )
            sid = create_runner_bound_session(client, agent_name=name, runner_id=stack.runner_id)
            configure_mock_llm(
                mock_url,
                [
                    _tool("workflow_prepare", "prepare", {"definition": {}}),
                    _tool("workflow_snapshot", "snapshot", {"workspace": str(repository)}),
                    {"text": "WORKER_READY"},
                ],
                key=model,
            )
            definition = {
                "workflow": "review-fix" if review_existing else "implement-review-fix",
                "repository": str(source),
                "task": "Check the greeting after reading source instructions.",
                "baseline": "HEAD",
                "base": "main",
                "candidates": [{"id": "current", "task": "Read source context for the greeting."}],
                "publish": False,
                "worktree_setup": False,
            }
            send_user_message_to_session(
                client, session_id=sid, content="/workflow start " + json.dumps(definition)
            )
            _wait_for_text(client, sid, "WORKER_READY")
            stack.restart_server()
            question = "Should the greeting include punctuation?"
            configure_mock_llm(
                mock_url,
                [
                    _tool("workflow_stage", "brief"),
                    _tool(
                        "workflow_read",
                        "instructions",
                        {"repository": "/wrong", "path": str(repository / ".codex/config.toml")},
                    ),
                    _tool(
                        "workflow_read",
                        "shared",
                        {"repository": "/wrong", "path": str(repository / "CLAUDE.local.md")},
                    ),
                    {
                        "text": json.dumps(
                            {
                                "stage": "current/" + stage,
                                "outcome": "NEEDS_INPUT",
                                "evidence": "Read shared source instructions; decision needed.",
                                "question": question,
                            }
                        ),
                        "block": True,
                    },
                ],
                key=(
                    "mock-repository-worker"
                    if review_existing
                    else "mock-repository-worker-implementation"
                ),
            )
            configure_mock_llm(
                mock_url,
                [
                    _tool("sys_session_send", "send"),
                    {"text": "WORKER_DISPATCHED"},
                    _tool("sys_read_inbox", "inbox"),
                    _tool("workflow_status", "status"),
                    {"text": "WORKER_WAITING"},
                ],
                key=model,
            )
            send_user_message_to_session(
                client, session_id=sid, content="Continue the approved workflow."
            )
            _wait_for_text(client, sid, "WORKER_DISPATCHED")
            _release_worker(mock_url)
            items = _wait_for_text(client, sid, "WORKER_WAITING")
            assert _status(items, "status")["status"] == "waiting-user"
            child_id = _status(items, "send")["conversation_id"]
            child_items = client.get(f"/v1/sessions/{child_id}/items").json()["data"]
            assert "Shared guidance" in _status(child_items, "instructions")["content"]
            assert "Original checkout instructions" in _status(child_items, "shared")["content"]
            assert _status(child_items, "instructions")["repository"] == str(source)
            assert (repository / "CLAUDE.local.md").read_text() == "Original checkout instructions"
            preview = client.get(f"/v1/sessions/{sid}/workflow-draft").json()
            assert question in preview["question"]["text"]
            stack.restart_server()
            assert client.get(f"/v1/sessions/{sid}/workflow-draft").json() == preview
