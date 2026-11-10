"""Review routing and decisions survive an isolated controller restart."""

import json
from pathlib import Path

import httpx
import pytest

from omnigent.cli import _bundle
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runner.tool_dispatch import _subagent_tool_result_policy_request
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.controller import STATE_KEY
from omnigent.workflows.operations import workflow_snapshot
from tests._helpers.server_runner import server_runner
from tests._helpers.session import post_session_bundle
from tests.e2e.test_workflow_controller_e2e import _mock_bundle
from tests.tools.test_workflow_operations import repository as repository


@pytest.mark.timeout(120)
@pytest.mark.parametrize("decision", [False, True])
def test_review_fix_and_decisions_survive_restart(repository, tmp_path, decision):
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
            snapshot = json.loads(workflow_snapshot(str(repository)))
            accepted = {
                "command": "/workflow answer Accept only legacy as an exception",
                "stage": "one/implement",
            }
            store.set_session_state(
                sid,
                {
                    STATE_KEY: {
                        "status": "ready",
                        "scheduler_version": 2,
                        "current": "one/review-a",
                        "attempt": 1,
                        "definition": {
                            "repository": str(repository),
                            "task": "Validate input",
                            "workflow": "review-fix",
                            "publish": False,
                        },
                        "candidates": [
                            {
                                "id": "one",
                                "workspace": str(repository),
                                "branch": "main",
                                "baseline": "HEAD",
                                "task": "Validate input",
                            }
                        ],
                        "revision": snapshot["revision"],
                        "pending": None,
                        "completed": {},
                        "verified": {},
                        "findings": {},
                        "fix_attempts": {},
                        "decisions": [accepted],
                        "answer": "Accept only legacy as an exception",
                    }
                },
            )

            def state():
                return store.get_conversation(sid).session_state[STATE_KEY]

            def evaluate(phase, data, request=None, session=sid):
                result = client.post(
                    f"/v1/sessions/{session}/policies/evaluate",
                    json={
                        "event": {
                            "type": phase,
                            "target": "",
                            "data": data,
                            "context": {},
                            "request_data": request,
                        }
                    },
                )
                result.raise_for_status()
                return result.json()

            def take_snapshot():
                return evaluate(
                    "PHASE_TOOL_RESULT",
                    {"result": workflow_snapshot(str(repository))},
                    {"name": "workflow_snapshot"},
                )

            def dispatch():
                result = evaluate("PHASE_TOOL_CALL", {"name": "sys_session_send", "arguments": {}})
                assert result["result"] == "POLICY_ACTION_ALLOW", result
                child = store.create_conversation(
                    parent_conversation_id=sid,
                    agent_id=store.get_conversation(sid).agent_id,
                    workspace=str(repository),
                    title="worker:" + result["data"]["title"],
                )
                return result["data"], child

            def complete(sent, child, findings=(), resolved=(), question=None):
                report = {
                    "stage": sent["title"].rsplit("/", 1)[0],
                    "outcome": "COMPLETE",
                    "evidence": "Inspected candidate and ran the required checks",
                    "findings": list(findings),
                    "resolved": list(resolved),
                }
                if question:
                    report["question"] = question
                result = client.post(
                    f"/v1/sessions/{sid}/policies/evaluate",
                    json=_subagent_tool_result_policy_request(
                        {
                            "agent": "worker",
                            "title": sent["title"],
                            "conversation_id": child.id,
                            "status": "completed",
                            "work_id": sent["title"],
                        },
                        json.dumps(report),
                    ),
                )
                result.raise_for_status()
                take_snapshot()

            findings = [{"id": "validation", "message": "Reject invalid input"}]
            question = (
                "Which input format? Two requirements conflict, "
                "so validation cannot be fixed safely."
            )
            if decision:
                findings.append({"id": "format", "message": "Conflicting input formats"})
            sent, child = dispatch()
            complete(sent, child, findings, question=question if decision else None)
            saved = state()
            assert saved["status"] == ("waiting-user" if decision else "snapshot")
            assert saved["current"] == ("one/review-a" if decision else "one/fix-a")
            stack.restart_server()
            assert state() == saved
            assert state()["decisions"] == [accepted]
            if decision:
                assert (
                    client.get(f"/v1/sessions/{sid}/workflow-draft")
                    .json()["question"]["text"]
                    .startswith("one/review-a")
                )
                evaluate("PHASE_REQUEST", {"text": "Status please"})
                assert state() == saved
                assert (
                    evaluate("PHASE_TOOL_CALL", {"name": "sys_session_send", "arguments": {}})[
                        "result"
                    ]
                    == "POLICY_ACTION_DENY"
                )
                assert (
                    evaluate(
                        "PHASE_REQUEST", {"text": "/workflow answer Use the existing format"}
                    )["result"]
                    == "POLICY_ACTION_ALLOW"
                )
                stack.restart_server()
                assert state()["decisions"][0] == accepted
                assert (
                    state()["decisions"][-1]["review"]["reports"]["one/review-a"]["findings"]
                    == findings
                )
            else:
                assert client.get(f"/v1/sessions/{sid}/workflow-draft").json()["question"] is None
            assert state()["findings"] == {item["id"]: item["message"] for item in findings}
            take_snapshot()
            sent, child = dispatch()
            assert sent["title"].startswith("one/fix-a/")
            brief = evaluate(
                "PHASE_TOOL_RESULT",
                {"result": "stub"},
                {"name": "workflow_stage"},
                session=child.id,
            )["data"]
            assert accepted["command"] in brief
            assert "Reject invalid input" in brief
            if decision:
                assert "Use the existing format" in brief
            (repository / "hello.txt").write_text("validated input\n")
            complete(sent, child, resolved=[item["id"] for item in findings])
            assert state()["current"] == "one/verify-a"
            assert state()["verified"] == {}
            assert (
                evaluate("PHASE_REQUEST", {"text": "/workflow select one"})["result"]
                == "POLICY_ACTION_DENY"
            )
            assert (
                evaluate("PHASE_TOOL_CALL", {"name": "workflow_publish", "arguments": {}})[
                    "result"
                ]
                == "POLICY_ACTION_DENY"
            )
            take_snapshot()
            sent, child = dispatch()
            complete(
                sent, child, [{"id": "validation", "message": "Invalid input still accepted"}]
            )
            assert state()["current"] == "one/fix-a"
            assert state()["status"] == "snapshot"
            assert state()["verified"] == {}
            assert state()["decisions"][0] == accepted

            take_snapshot()
            sent, child = dispatch()
            (repository / "hello.txt").write_text("validated all inputs\n")
            complete(sent, child, resolved=list(state()["findings"]))
            saved = state()
            stack.restart_server()
            assert state() == saved
            take_snapshot()
            sent, child = dispatch()
            assert sent["title"].startswith("one/verify-a/")
            complete(sent, child, resolved=list(state()["findings"]))
            take_snapshot()
            standards, standards_child = dispatch()
            spec, spec_child = dispatch()
            complete(standards, standards_child)
            complete(spec, spec_child)
            assert state()["current"] is None
            assert state()["status"] == "waiting-user"
            assert state()["decisions"][0] == accepted
            evidence = state()["completed"]
            generated = repository / "graphify-out"
            generated.mkdir()
            (generated / "graph.json").write_text('{"nodes": []}\n')
            evaluate("PHASE_REQUEST", {"text": "/workflow select one"})
            take_snapshot()
            assert state()["status"] == "complete"
            assert state()["completed"] == evidence
            assert state()["scheduling"][-1]["action"] == "retain-evidence"
            assert "graphify-out/graph.json" in state()["snapshots"]["one"]["files"]
            assert (
                state()["verified"]["one"]
                == json.loads(workflow_snapshot(str(repository)))["revision"]
            )
            saved = state()
            stack.restart_server()
            assert state() == saved
            assert (
                evaluate("PHASE_TOOL_CALL", {"name": "sys_session_send", "arguments": {}})[
                    "result"
                ]
                == "POLICY_ACTION_DENY"
            )
            (generated / "graph.json").write_text('{"nodes": [1]}\n')
            (repository / "hello.txt").write_text("mixed source change\n")
            evaluate("PHASE_REQUEST", {"text": "/workflow publish one"})
            take_snapshot()
            assert state()["selected"] is None
            assert state()["verified"] == {}
            assert state()["current"] == "one/review-a"
            assert state()["status"] == "snapshot"
