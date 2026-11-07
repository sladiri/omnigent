"""A review worker journals through real runner tools without changing its source."""

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.workflows.operations import _git
from tests._helpers.operator_agents import operator_agent_store
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
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
def test_review_journals_in_main_checkout_and_keeps_wait_after_restart(
    repository, isolated_mock_llm_server_url, tmp_path
):
    mock_url = isolated_mock_llm_server_url
    model = "mock-journal-controller"
    bundle = _mock_bundle(tmp_path / "bundle", "mock-journal-worker", f"{mock_url}/v1")
    main = repository
    beads = main / ".beads"
    beads.mkdir()
    (beads / "beads.db").touch()
    (beads / "PRIME.md").write_text("Use the existing session journal.")
    repository = tmp_path / "candidate"
    _git(main, "worktree", "add", "-b", "candidate", str(repository))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    br = fakebin / "br"
    br.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"assert Path.cwd() == Path({str(main)!r})\n"
        f"assert args[:3] == ['--db', {str(beads / 'beads.db')!r}, '--json']\n"
        "if args[3] == 'show':\n"
        "    print(json.dumps([{'id':'test-1','labels':['session','agent-journal']}]))\n"
        "else:\n"
        "    assert args[3:6] == ['comments','add','test-1']\n"
        "    assert args[6] == '--message'\n"
        f"    Path({str(beads / 'recorded.json')!r}).write_text(json.dumps(args[7]))\n"
        "    print(json.dumps({'recorded': True}))\n"
    )
    br.chmod(0o755)
    service = main / "services/app"
    service.mkdir(parents=True)
    with (
        server_runner(
            tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2], workspace=service
        ) as stack,
        operator_agent_store(stack.base_url, stack.database_uri, stack.artifact_location),
    ):
        stack.start_runner(env={"PATH": str(fakebin) + os.pathsep + os.environ["PATH"]})
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            name = register_dir_agent_with_mock_llm(
                client,
                agent_dir=bundle,
                name="journal-review",
                model=model,
                mock_llm_base_url=f"{mock_url}/v1",
            )
            response = client.post(
                "/v1/sessions",
                json={"agent_id": lookup_agent_id(client, name), "workspace": str(service)},
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            )
            response.raise_for_status()
            sid = response.json()["id"]
            bind_session_runner(client.patch, "", sid, stack.runner_id)
            for attempt in range(2):
                configure_mock_llm(
                    mock_url,
                    [
                        _tool(
                            "workflow_journal",
                            f"idle-instructions-{attempt}",
                            {"repository": "/wrong", "action": "instructions"},
                        ),
                        _tool(
                            "workflow_journal",
                            f"idle-comment-{attempt}",
                            {
                                "repository": "/wrong",
                                "action": "comment",
                                "issue": "test-1",
                                "text": "[verification] Preparation journal from a subfolder.",
                            },
                        ),
                        _tool("workflow_status", f"idle-status-{attempt}"),
                        {"text": f"PREPARATION_JOURNALED_{attempt}"},
                    ],
                    key=model,
                )
                send_user_message_to_session(
                    client, session_id=sid, content="Record preparation. Do not start the review."
                )
                items = _wait_for_text(client, sid, f"PREPARATION_JOURNALED_{attempt}")
                assert _status(items, f"idle-instructions-{attempt}")["repository"] == str(main)
                assert _status(items, f"idle-comment-{attempt}") == {"recorded": True}
                assert _status(items, f"idle-status-{attempt}")["status"] == "idle"
                assert json.loads((beads / "recorded.json").read_text()) == (
                    "[verification] Preparation journal from a subfolder."
                )
                assert not (service / ".beads").exists()
                stack.restart_server()
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
                    _tool(
                        "workflow_journal",
                        "instructions",
                        {"repository": "/wrong", "action": "instructions"},
                    ),
                    _tool(
                        "workflow_journal",
                        "journal",
                        {
                            "repository": "/wrong",
                            "action": "comment",
                            "issue": "test-1",
                            "text": "[verification] Review completed; user decision required.",
                        },
                    ),
                    {
                        "text": json.dumps(
                            {
                                "stage": "current/review-a",
                                "outcome": "NEEDS_INPUT",
                                "evidence": "Inspected source and recorded review evidence.",
                                "question": question,
                            }
                        ),
                        "block": True,
                    },
                ],
                key="mock-journal-worker",
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
            assert _status(child_items, "instructions")["repository"] == str(main)
            assert (
                _status(child_items, "instructions")["instructions"]
                == "Use the existing session journal."
            )
            assert _status(child_items, "journal") == {"recorded": True}
            assert (
                json.loads((beads / "recorded.json").read_text())
                == "[verification] Review completed; user decision required."
            )
            assert not (repository / ".beads").exists()
            assert _git(repository, "status", "--porcelain") == ""
            preview = client.get(f"/v1/sessions/{sid}/workflow-draft").json()
            assert question in preview["question"]["text"]
            stack.restart_server()
            assert client.get(f"/v1/sessions/{sid}/workflow-draft").json() == preview
