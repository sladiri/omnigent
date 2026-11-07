"""Installed skill reads and the Answer prompt through a real isolated worker."""

import json
from pathlib import Path

import httpx
import pytest

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


@pytest.mark.timeout(150)
def test_review_reads_installed_skills_and_exposes_answer_after_restart(
    repository, isolated_mock_llm_server_url, tmp_path
):
    mock_url = isolated_mock_llm_server_url
    model = "mock-skill-controller"
    bundle = _mock_bundle(tmp_path / "bundle", "mock-skill-worker", f"{mock_url}/v1")
    with (
        server_runner(tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2]) as stack,
        operator_agent_store(stack.base_url, stack.database_uri, stack.artifact_location),
    ):
        skills = []
        for name in ("ponytail", "ponytail-review", "caveman-commit", "diagnosing-bugs", "tdd"):
            source = tmp_path / "skill-repository" / name
            source.mkdir(parents=True)
            skill = source / "SKILL.md"
            skill.write_text("Installed review instructions: " + name)
            skills.append(skill)
            reference = source / "reference.md"
            reference.write_text("Installed skill reference: " + name)
            skills.append(reference)
            for home in (stack.server_home, stack.runner_home):
                link = home / ".agents/skills" / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(source, target_is_directory=True)
        stack.start_runner()
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            name = register_dir_agent_with_mock_llm(
                client,
                agent_dir=bundle,
                name="skill-review",
                model=model,
                mock_llm_base_url=f"{mock_url}/v1",
            )
            sid = create_runner_bound_session(client, agent_name=name, runner_id=stack.runner_id)
            configure_mock_llm(
                mock_url,
                [
                    _tool("workflow_prepare", "prepare", {"definition": {}}),
                    _tool("workflow_snapshot", "snapshot", {"workspace": str(repository)}),
                    {"text": "REVIEW_READY"},
                ],
                key=model,
            )
            definition = {
                "workflow": "review-fix",
                "repository": str(repository),
                "task": "Review the existing greeting using the installed review skills.",
                "baseline": "HEAD",
                "base": "main",
                "candidates": [{"id": "current", "task": "Review the existing greeting."}],
                "publish": False,
                "worktree_setup": False,
            }
            send_user_message_to_session(
                client, session_id=sid, content="/workflow start " + json.dumps(definition)
            )
            _wait_for_text(client, sid, "REVIEW_READY")
            stack.restart_server()
            question = "Should the greeting include punctuation?"
            configure_mock_llm(
                mock_url,
                [
                    _tool("workflow_stage", "brief"),
                    *[
                        _tool(
                            "sys_os_read",
                            skill.parent.name + "-" + skill.stem,
                            {"path": str(skill)},
                        )
                        for skill in skills
                    ],
                    {
                        "text": json.dumps(
                            {
                                "stage": "current/review-a",
                                "outcome": "NEEDS_INPUT",
                                "evidence": "Read allowlisted global skills and their references.",
                                "question": question,
                            }
                        ),
                        "block": True,
                    },
                ],
                key="mock-skill-worker",
            )
            configure_mock_llm(
                mock_url,
                [
                    _tool("sys_session_send", "send"),
                    {"text": "REVIEW_DISPATCHED"},
                    _tool("sys_read_inbox", "inbox"),
                    _tool("workflow_status", "status"),
                    {"text": "REVIEW_WAITING"},
                ],
                key=model,
            )
            send_user_message_to_session(client, session_id=sid, content="Begin the review.")
            _wait_for_text(client, sid, "REVIEW_DISPATCHED")
            _release_worker(mock_url)
            items = _wait_for_text(client, sid, "REVIEW_WAITING")
            assert _status(items, "status")["status"] == "waiting-user"
            child_id = _status(items, "send")["conversation_id"]
            child_items = client.get(f"/v1/sessions/{child_id}/items").json()["data"]
            for skill in skills:
                output = next(
                    item["output"]
                    for item in child_items
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == skill.parent.name + "-" + skill.stem
                )
                assert skill.read_text() in output, output
                assert "Error:" not in output
            preview = client.get(f"/v1/sessions/{sid}/workflow-draft").json()
            assert question in preview["question"]["text"]
            stack.restart_server()
            assert client.get(f"/v1/sessions/{sid}/workflow-draft").json() == preview
