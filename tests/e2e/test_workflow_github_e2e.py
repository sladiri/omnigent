"""PR checkout, saved start approval and GitHub reads through an isolated workflow."""

from __future__ import annotations

import json
import os
import sys
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
from tests.e2e.test_workflow_controller_e2e import _mock_bundle, _status, _tool, _wait_for_text
from tests.tools.test_workflow_operations import repository as repository
from tests.tools.test_workflow_operations import review_pr as review_pr


@pytest.mark.timeout(120)
def test_pr_checkout_prepares_draft_and_preserves_start_gate_after_restart(
    review_pr,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
):
    repo, metadata, baseline, _ = review_pr
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    gh = fakebin / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\nassert sys.argv[1:3] == ['pr', 'view']\n"
        f"print({json.dumps(metadata)!r})\n"
    )
    gh.chmod(0o755)
    mock_url = isolated_mock_llm_server_url
    checkout = str(repo.parent / f"{repo.name}-pr-303")
    bundle = _mock_bundle(tmp_path / "bundle", "mock-github-worker", f"{mock_url}/v1")
    model = "mock-github-controller"
    with (
        server_runner(tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2]) as stack,
        operator_agent_store(stack.base_url, stack.database_uri, stack.artifact_location),
    ):
        stack.start_runner(env={"PATH": str(fakebin) + os.pathsep + os.environ["PATH"]})
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            name = register_dir_agent_with_mock_llm(
                client,
                agent_dir=bundle,
                name="github-review",
                model=model,
                mock_llm_base_url=f"{mock_url}/v1",
            )
            sid = create_runner_bound_session(client, agent_name=name, runner_id=stack.runner_id)
            configure_mock_llm(
                mock_url,
                [
                    _tool(
                        "workflow_review_checkout",
                        "checkout",
                        {"repository": str(repo), "pull_request_url": metadata["url"]},
                    ),
                    _tool(
                        "workflow_draft",
                        "draft",
                        {
                            "workflow": "review-fix",
                            "repository": checkout,
                            "task": "Review the PR against current service conventions.",
                            "baseline": baseline,
                            "base": "main",
                            "branch": metadata["headRefName"],
                            "publish": False,
                        },
                    ),
                    _tool("workflow_prepare", "premature-prepare", {"definition": {}}),
                    _tool("workflow_status", "draft-status"),
                    {"text": "PR_DRAFT_READY"},
                ],
                key=model,
            )
            send_user_message_to_session(
                client,
                session_id=sid,
                content=(
                    "Prepare the start message for `review-fix` using the task below.\n\n"
                    "Review " + metadata["url"]
                ),
            )
            items = _wait_for_text(client, sid, "PR_DRAFT_READY")
            assert _status(items, "checkout")["head"] == metadata["headRefOid"]
            assert _status(items, "draft-status")["status"] == "idle"
            assert "Denied by policy" in json.dumps(items)
            preview = client.get(f"/v1/sessions/{sid}/workflow-draft").json()["draft"]
            assert preview["repository"] == checkout and preview["baseline"] == baseline
            stack.restart_server()
            assert client.get(f"/v1/sessions/{sid}/workflow-draft").json()["draft"] == preview
            for separator in ("_", "__"):
                for operation, allowed in (
                    ("get_pr_info", True),
                    ("fetch_file", True),
                    ("create_branch", False),
                    ("add_review_to_pr", False),
                ):
                    response = client.post(
                        f"/v1/sessions/{sid}/policies/evaluate",
                        json={
                            "event": {
                                "type": "PHASE_TOOL_CALL",
                                "target": "",
                                "context": {},
                                "data": {
                                    "name": f"mcp__codex_apps__github{separator}{operation}",
                                    "arguments": {
                                        "repository_full_name": "org/repo",
                                        "pr_number": 303,
                                    },
                                },
                            }
                        },
                    )
                    response.raise_for_status()
                    assert response.json()["result"] == "POLICY_ACTION_" + (
                        "ALLOW" if allowed else "DENY"
                    )
            assert client.get(f"/v1/sessions/{sid}/workflow-draft").json()["draft"] == preview
            configure_mock_llm(
                mock_url,
                [
                    _tool("workflow_prepare", "approved-prepare", {"definition": {}}),
                    _tool("workflow_snapshot", "snapshot", {"workspace": checkout}),
                    _tool(
                        "workflow_review_checkout",
                        "late-checkout",
                        {"repository": str(repo), "pull_request_url": metadata["url"]},
                    ),
                    _tool("workflow_status", "approved-status"),
                    {"text": "PR_REVIEW_READY"},
                ],
                key=model,
            )
            send_user_message_to_session(
                client, session_id=sid, content="/workflow start-draft " + preview["token"]
            )
            items = _wait_for_text(client, sid, "PR_REVIEW_READY")
            state = _status(items, "approved-status")
            assert state["status"] == "ready" and state["current"] == "current/review-a"
            assert state["candidates"][0]["workspace"] == checkout
            assert state["pending"] is None
            assert state["definition"]["publish"] is False
