"""Sibling Git permissions survive an isolated controller server restart."""

import copy
import shlex
import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.cli import _bundle
from omnigent.native.native_policy_hook import hook_payload_to_evaluation_request
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.controller import STATE_KEY
from tests._helpers.server_runner import server_runner
from tests._helpers.session import post_session_bundle
from tests.e2e.test_workflow_controller_e2e import _mock_bundle
from tests.tools.test_workflow_operations import repository as repository


@pytest.mark.timeout(90)
def test_sibling_creation_through_native_hook_preserves_state_after_restart(repository, tmp_path):
    bundle = _mock_bundle(tmp_path / "bundle", "mock-worker", "http://127.0.0.1:1/v1")
    with server_runner(
        tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2]
    ) as stack:
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                _bundle(bundle),
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            )
            response.raise_for_status()
            sid = response.json()["session_id"]
            store = SqlAlchemyConversationStore(stack.database_uri)
            parent = store.get_conversation(sid)
            child = store.create_conversation(
                parent_conversation_id=sid,
                agent_id=parent.agent_id,
                workspace=str(repository),
                sub_agent_name="worker",
                title="worker:one/implement/1",
            )
            state = {
                "status": "running",
                "current": "one/implement",
                "definition": {"repository": str(repository), "publish": False},
                "pending": {
                    "stage": "one/implement",
                    "title": "one/implement/1",
                    "child_id": child.id,
                },
                "candidates": [{"id": "one", "workspace": str(repository)}],
                "completed": {},
                "verified": {},
                "selected": None,
            }
            store.set_session_state(sid, {STATE_KEY: state})

            def permission(command):
                request = hook_payload_to_evaluation_request(
                    "PreToolUse",
                    {
                        "tool_name": "exec_command",
                        "tool_input": {"cmd": f"cd {shlex.quote(str(repository))} && " + command},
                        "cwd": str(repository),
                    },
                )
                result = client.post(f"/v1/sessions/{child.id}/policies/evaluate", json=request)
                result.raise_for_status()
                return result.json()["result"]

            command = "git worktree add -b adjacent ../adjacent HEAD"
            assert permission(command) == "POLICY_ACTION_ALLOW"
            read = "git --no-pager show HEAD:deploy/k8s/tests/push-render.sh"
            assert permission(read) == "POLICY_ACTION_ALLOW"
            stack.restart_server()
            assert permission(command) == "POLICY_ACTION_ALLOW"
            assert permission(read) == "POLICY_ACTION_ALLOW"
            assert permission(read + " && git commit -am change") == "POLICY_ACTION_DENY"
            subprocess.run(shlex.split(command), cwd=repository, check=True, capture_output=True)
            assert (repository.parent / "adjacent/hello.txt").read_text() == "hello\n"
            assert permission(command) == "POLICY_ACTION_DENY"
            assert permission("git merge-base --is-ancestor HEAD main") == "POLICY_ACTION_ALLOW"
            assert permission("git merge main") == "POLICY_ACTION_DENY"
            assert store.get_conversation(sid).session_state[STATE_KEY] == state
            paused = copy.deepcopy(state)
            paused["status"] = "waiting-user"
            store.set_session_state(sid, {STATE_KEY: paused})
            stack.restart_server()
            assert permission("git worktree add ../another") == "POLICY_ACTION_DENY"
            assert permission(read) == "POLICY_ACTION_DENY"
            assert store.get_conversation(sid).session_state[STATE_KEY] == paused
