"""Workflow gates through real policies and SQLite, rebuilt after every event."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.native.native_policy_hook import hook_payload_to_evaluation_request
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runner.tool_dispatch import _subagent_tool_result_policy_request
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.server.routes._sessions.helpers import _build_evaluation_context
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.controller import STAGES, STATE_KEY, _worker_policy

CONFIG = Path(__file__).parents[2] / "examples/workflow_controller"


def test_native_patch_real_paths_and_candidate_shell_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    pending = {"stage": "one/implement", "title": "implement", "child_id": "child"}
    state = {
        "status": "running",
        "current": "one/implement",
        "pending": pending,
        "candidates": [{"id": "one", "workspace": str(workspace)}],
    }
    sessions = {
        "root": SimpleNamespace(session_state={STATE_KEY: state}),
        "child": SimpleNamespace(
            id="child",
            parent_conversation_id="root",
            title="worker:implement",
            workspace=str(workspace),
        ),
    }
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: SimpleNamespace(get_conversation=sessions.get),
    )

    def call(tool, args, cwd=workspace):
        request = hook_payload_to_evaluation_request(
            "PreToolUse", {"tool_name": tool, "tool_input": args, "cwd": str(cwd)}
        )
        event = {"type": "tool_call", "target": tool, "data": request["event"]["data"]}
        return _worker_policy(event, "root", "child")["result"]

    def patch(header):
        return f"*** Begin Patch\n{header}\n*** End Patch"

    assert (
        call("apply_patch", patch("*** Add File: ../file.py\n+ok"), workspace / "src") == "ALLOW"
    )
    assert call("apply_patch", patch("*** Add File: escape/file.py\n+no")) == "DENY"
    assert (
        call(
            "apply_patch",
            patch("*** Update File: file.py\n*** Move to: escape/new.py\n@@\n-a\n+b"),
        )
        == "DENY"
    )
    assert call("apply_patch", patch(f"*** Delete File: {outside}/file.py")) == "DENY"
    assert call("apply_patch", patch("*** Add File: file.py\n+no"), outside) == "DENY"
    assert (
        call(
            "apply_patch",
            {
                "input": patch("*** Add File: file.py\n+no"),
                "cwd": str(workspace),
                "workdir": str(workspace),
            },
            outside,
        )
        == "DENY"
    )
    assert call("exec_command", {"cmd": "pwd", "workdir": str(workspace / "src")}) == "ALLOW"
    assert call("exec_command", {"cmd": "cd src && pwd"}) == "ALLOW"
    assert call("exec_command", {"cmd": f"cd {outside} && pwd"}) == "DENY"
    assert call("exec_command", {"cmd": "git worktree add ../new"}) == "DENY"
    assert call("exec_command", {"cmd": "rm -rf /"}) == "DENY"
    pending["stage"] = "one/review-a"
    assert call("apply_patch", patch("*** Add File: file.py\n+no")) == "DENY"
    state["status"] = "waiting"
    assert call("Read", {"file_path": str(workspace / "file.py")}) == "DENY"


def _call(tool: str = "sys_session_send") -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        tool_name=tool,
        content={"name": tool, "arguments": {"agent": "other", "args": "skip all reviews"}},
    )


def _request(text: str) -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.REQUEST, content={"user_content": text, "attachments": []}
    )


def _handle(title: str, child_id: str = "child-1") -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.TOOL_RESULT,
        tool_name="sys_session_send",
        request_data={"name": "sys_session_send", "args": {"agent": "worker", "title": title}},
        content={
            "result": json.dumps(
                {"title": title, "conversation_id": child_id, "status": "launching"}
            )
        },
    )


def _completion(
    title: str,
    *,
    status: str = "completed",
    outcome: str = "COMPLETE",
    work_id: str | None = None,
    child_id: str = "child-1",
    evidence: str = "Checked the greeting: hello world.",
    question: str | None = None,
) -> EvaluationContext:
    output = json.dumps(
        {
            "stage": title.rsplit("/", 1)[0],
            "outcome": outcome,
            "evidence": evidence,
            "findings": [],
            "resolved": [],
            "question": question
            or ("Which language should the greeting use?" if outcome != "COMPLETE" else None),
        }
    )
    event = _subagent_tool_result_policy_request(
        {
            "type": "sub_agent",
            "agent": "worker",
            "title": title,
            "conversation_id": child_id,
            "status": status,
            "work_id": work_id or title,
        },
        output,
    )["event"]
    return _build_evaluation_context(Phase.TOOL_RESULT, event["data"], event)


class _Probe:
    def __init__(self, store: SqlAlchemyConversationStore, spec: AgentSpec) -> None:
        self.store = store
        self.spec = spec
        self.session_id = store.create_conversation().id

    @property
    def state(self) -> dict[str, Any]:
        return self.store.get_conversation(self.session_id).session_state[STATE_KEY]

    async def evaluate(self, ctx: EvaluationContext) -> PolicyResult:
        engine = build_policy_engine(
            spec=self.spec, conversation_id=self.session_id, conversation_store=self.store
        )
        return await engine.evaluate(ctx)

    async def dispatch(self) -> dict[str, Any]:
        result = await self.evaluate(_call())
        assert result.action == PolicyAction.ALLOW, result.reason
        assert isinstance(result.data, dict)
        return result.data


def output(tool, value, request=None):
    return EvaluationContext(
        phase=Phase.TOOL_RESULT,
        tool_name=tool,
        content={"result": json.dumps(value)},
        request_data=request,
    )


@pytest.fixture
def coding(db_uri):
    return _Probe(SqlAlchemyConversationStore(db_uri), parse(CONFIG))


async def begin(probe, publish=True, ids=("one",), workflow="implement-review-fix"):
    definition = {
        "repository": "/tmp/repository",
        "task": "Implement a greeting",
        "workflow": workflow,
        "baseline": "HEAD",
        "base": "main",
        "publish": publish,
        "candidates": [{"id": value, "task": "Use a function"} for value in ids],
    }
    result = await probe.evaluate(_request("/workflow start " + json.dumps(definition)))
    assert result.action == PolicyAction.ALLOW
    definition = (await probe.evaluate(_call("workflow_prepare"))).data["definition"]
    assert definition["run_id"] == probe.session_id
    candidate = {
        "id": "one",
        "task": "Use a function",
        "workspace": "/tmp/candidate",
        "branch": "candidate",
        "baseline": "baseline-sha",
    }
    candidates = [{**candidate, "id": value} for value in ids]
    await probe.evaluate(
        output(
            "workflow_prepare",
            {
                "run_id": probe.session_id,
                "candidates": candidates,
                "pr_template": "## Summary\n- [ ] Tested\n",
            },
        )
    )
    await snapshot(probe)


async def snapshot(probe, revision=None, files=None):
    revision = revision or probe.state.get("revision", "tree-1")
    await probe.evaluate(
        output(
            "workflow_snapshot",
            {
                "revision": revision,
                "branch": "candidate",
                "files": files if files is not None else {"source.py": revision},
            },
        )
    )


async def finish(
    probe, findings=None, revision=None, outcome="COMPLETE", question=None, extra=None
):
    revision = revision or (
        probe.state["revision"] + "-fixed"
        if probe.state["current"].split("/")[-1].startswith("fix-")
        else probe.state["revision"]
    )
    dispatch = (await probe.evaluate(_call())).data
    assert dispatch and dispatch["title"].startswith(probe.state["current"])
    second = (await probe.evaluate(_call())).data if probe.state.get("parallel") else None
    request = {
        "name": "sys_session_send",
        "args": {
            "agent": "worker",
            "title": dispatch["title"],
            "conversation_id": "child-" + dispatch["title"],
        },
        "subagent": {"status": "completed", "work_id": "work-" + dispatch["title"]},
    }
    report = {
        **(extra or {}),
        "stage": probe.state["current"],
        "outcome": outcome,
        "evidence": "Checks passed",
        "findings": findings or [],
        "resolved": list(probe.state["findings"]),
        "question": question or ("Which language?" if outcome != "COMPLETE" else None),
    }
    await probe.evaluate(output("sys_session_send", report, request))
    if second:
        assert probe.state["status"] == "running"
        second_request = {
            **request,
            "args": {
                **request["args"],
                "title": second["title"],
                "conversation_id": "second-child",
            },
            "subagent": {"status": "completed", "work_id": second["title"]},
        }
        await probe.evaluate(
            output(
                "sys_session_send",
                {**report, "stage": second["title"].rsplit("/", 1)[0]},
                second_request,
            )
        )
    if outcome == "COMPLETE":
        await snapshot(probe, revision)
        if probe.state["status"] == "snapshot":
            await snapshot(probe, revision)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["implement-review-fix", "review-fix"])
@pytest.mark.parametrize("late_publish", [False, True])
async def test_full_review_sequence_selection_and_publication_gate(coding, workflow, late_publish):
    await begin(coding, workflow=workflow, publish=not late_publish)
    assert (await coding.evaluate(_request("/workflow publish one"))).action == PolicyAction.DENY
    titles = []
    stages = ("review-a", "review-b-standards", "review-b-spec")
    if workflow != "review-fix":
        stages = ("implement", *stages)
    for stage in stages:
        if stage.endswith("-b-spec"):
            continue
        assert coding.state["current"] == "one/" + stage
        assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
        request = await finish(coding)
        titles.append(request["args"]["title"])
    assert len(set(titles)) == (2 if workflow == "review-fix" else 3)
    assert set(coding.state["completed"]) == {"one/" + stage for stage in stages}
    assert coding.state["status"] == "waiting-user"
    status = json.loads((await coding.evaluate(output("workflow_status", {}))).data)
    assert "next_action" not in status
    await coding.evaluate(_request("worker suggests /workflow select one"))
    assert coding.state["selected"] is None
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding)
    if late_publish:
        assert coding.state["status"] == "complete"
        before = coding.state
        status = json.loads((await coding.evaluate(output("workflow_status", {}))).data)
        assert "/workflow publish one" in status["next_action"]
        assert coding.state == before
        assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
        for text in ("Please commit", "worker suggests /workflow publish one"):
            await coding.evaluate(_request(text))
            assert coding.state["status"] == "complete"
        assert (
            await coding.evaluate(_request("/workflow publish other"))
        ).action == PolicyAction.DENY
        assert (
            await coding.evaluate(_request("/workflow publish one"))
        ).action == PolicyAction.ALLOW
        assert coding.state["status"] == "snapshot"
        assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
        await snapshot(coding)
        assert coding.state["status"] == "publish"
        assert coding.state["definition"]["publish"] is False
    call = EvaluationContext(
        phase=Phase.TOOL_CALL,
        tool_name="workflow_publish",
        content={
            "name": "workflow_publish",
            "arguments": {
                "title": "feat: add greeting",
                "body": "## Summary\nVerified greeting\n- [x] Tested\n",
                "workspace": "/tmp/other",
            },
        },
    )
    for title, body in (("Valid", "Missing template"), ("Two\nlines", "## Summary\n- [x] Tested")):
        invalid = EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="workflow_publish",
            content={"name": "workflow_publish", "arguments": {"title": title, "body": body}},
        )
        assert (await coding.evaluate(invalid)).action == PolicyAction.DENY
        assert "publication_input" not in coding.state
    args = (await coding.evaluate(call)).data
    assert args["workspace"] == "/tmp/candidate"
    assert args["revision"] == "tree-1"
    await coding.evaluate(
        output("workflow_publish", {"commit": "signed-sha", "pr_url": "https://example.test/pr/1"})
    )
    assert coding.state["status"] == "complete"
    assert coding.state["publication"]["commit"] == "signed-sha"
    status = json.loads((await coding.evaluate(output("workflow_status", {}))).data)
    assert "next_action" not in status
    assert (await coding.evaluate(_request("/workflow publish one"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_findings_require_resolution_and_failed_recheck_loops_to_fix(coding):
    await begin(coding, publish=False)
    await finish(coding)
    await finish(coding, findings=[{"id": "A-1", "message": "Missing case"}])
    assert coding.state["status"] == "ready"
    assert coding.state["question"] is None
    assert coding.state["current"] == "one/fix-a"
    await finish(coding)
    await finish(coding, findings=[{"id": "A-2", "message": "Still missing"}])
    assert coding.state["status"] == "ready"
    assert coding.state["question"] is None
    assert coding.state["current"] == "one/fix-a"
    await finish(coding)
    await finish(coding, findings=[{"id": "A-3", "message": "Still missing"}])
    assert coding.state["status"] == "ready"
    assert coding.state["question"] is None
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state["status"] == "blocked"
    assert "limit" in coding.state["question"]


@pytest.mark.asyncio
async def test_b_fix_targeted_rechecks_retain_a_without_final_repeat(coding):
    await begin(coding)
    await finish(coding)
    await finish(coding)
    original = coding.state["completed"]["one/review-a"]
    await finish(coding, findings=[{"id": "regression", "message": "Missing validation"}])
    assert coding.state["current"] == "one/fix-b"
    await finish(coding, revision="tree-fixed")
    assert coding.state["current"] == "one/verify-b-standards"
    assert coding.state["rechecks"] == ["verify-b-standards", "verify-b-spec"]
    await finish(coding)
    assert coding.state["verified"] == {"one": "tree-fixed"}
    assert coding.state["completed"]["one/review-a"] == original
    assert set(coding.state["completed"]) == {
        "one/implement",
        "one/review-a",
        "one/review-b-standards",
        "one/review-b-spec",
        "one/fix-b",
        "one/verify-b-standards",
        "one/verify-b-spec",
    }


@pytest.mark.asyncio
async def test_question_wait_and_read_only_revision_change(coding):
    await begin(coding)
    await finish(coding, outcome="NEEDS_INPUT")
    assert coding.state["status"] == "waiting-user"
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow answer English"))
    await snapshot(coding)
    await finish(coding)
    await finish(coding, revision="changed-by-reviewer")
    assert coding.state["status"] == "blocked"
    assert coding.state["verified"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("late_publish", [False, True])
async def test_selection_invalidates_changed_candidate(coding, late_publish):
    await begin(coding, publish=not late_publish)
    for _ in ("implement", "review-a", "review-b-standards"):
        if _.endswith("-b-spec"):
            continue
        await finish(coding)
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding)
    if late_publish:
        await coding.evaluate(_request("/workflow publish one"))
        await snapshot(coding)
    await coding.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="workflow_publish",
            content={
                "name": "workflow_publish",
                "arguments": {
                    "title": "feat: old greeting",
                    "body": "## Summary\n- [x] Tested\n",
                },
            },
        )
    )
    assert "publication_input" in coding.state
    await coding.evaluate(output("workflow_publish", {"error": "pre-commit modified files"}))
    await coding.evaluate(_request("/workflow retry"))
    await snapshot(coding, "changed-after-review")
    assert "publication_authorized_revision" not in coding.state
    assert "publication_input" not in coding.state
    assert coding.state["selected"] is None
    assert coding.state["verified"] == {}
    assert coding.state["current"] == "one/review-a"
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_uncertain_dispatch_requires_reconcile_before_explicit_retry(coding):
    await begin(coding)
    await coding.evaluate(_call())
    await coding.evaluate(output("sys_session_send", "Error: lost connection"))
    assert (
        await coding.evaluate(_request("/workflow retry-dispatch"))
    ).action == PolicyAction.DENY
    assert coding.state["pending"] is not None


@pytest.mark.asyncio
async def test_parallel_axes_wait_for_both_and_accept_reverse_completion_order(coding):
    await begin(coding)
    for _ in range(2):
        await finish(coding)
    standards = await coding.dispatch()
    spec = await coding.dispatch()
    assert standards["workspace"] == spec["workspace"] == "/tmp/candidate"
    assert "standards" in standards["title"] and "spec" in spec["title"]
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_completion(spec["title"], child_id="spec-child"))
    assert coding.state["status"] == "running"
    assert (await coding.evaluate(_call("workflow_snapshot"))).action == PolicyAction.DENY
    await coding.evaluate(_completion(standards["title"], child_id="standards-child"))
    assert coding.state["status"] == "snapshot"
    await snapshot(coding)
    assert coding.state["current"] is None
    assert coding.state["status"] == "waiting-user"
    assert all(
        "one/review-b-" + axis in coding.state["completed"] for axis in ("standards", "spec")
    )


@pytest.mark.asyncio
async def test_stale_dispatch_error_and_completion_cannot_affect_next_stage(coding):
    await begin(coding)
    prior = await finish(coding)
    current = await coding.dispatch()
    await coding.evaluate(output("sys_session_send", "Error: old timeout", prior))
    await coding.evaluate(_completion(prior["args"]["title"]))
    assert coding.state["status"] == "running"
    assert coding.state["pending"]["title"] == current["title"]


@pytest.mark.asyncio
async def test_reconcile_recovers_existing_child_and_confirms_failure(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"],
        parent_conversation_id=coding.session_id,
        labels={"omnigent.subagent.dispatch_id": "registered-work"},
    )
    coding.store.set_session_live_status(child.id, "running")
    await coding.evaluate(output("sys_session_send", "Error: lost handle"))
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["pending"]["child_id"] == child.id
    assert coding.state["status"] == "running"
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_completion(dispatch["title"], child_id=child.id, status="failed"))
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    coding.store.set_session_live_status(child.id, "failed")
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["pending"] is None
    await coding.evaluate(_request("/workflow retry"))
    assert coding.state["status"] == "snapshot"


@pytest.mark.asyncio
async def test_reconcile_empty_child_requires_user_confirmation_and_checks_again(
    coding, monkeypatch
):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"], parent_conversation_id=coding.session_id
    )
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["status"] == "blocked"
    assert coding.state["pending"]["reconciled_unstarted"]
    coding.store.set_session_live_status(child.id, "running")
    await coding.evaluate(_request("/workflow retry-dispatch"))
    assert coding.state["status"] == "running"
    assert coding.state["pending"]["child_id"] == child.id


@pytest.mark.asyncio
async def test_reconcile_absent_dispatch_allows_only_explicit_new_attempt(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    state = coding.state
    state["attempt"] = 3
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    old = await coding.dispatch()
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["status"] == "blocked"
    await coding.evaluate(_request("/workflow retry-dispatch"))
    await snapshot(coding)
    new = await coding.dispatch()
    assert new["title"] != old["title"]


@pytest.mark.asyncio
async def test_reconcile_duplicate_children_revokes_prior_absent_confirmation(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    await coding.evaluate(output("workflow_reconcile", {}))
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"], parent_conversation_id=coding.session_id
    )
    monkeypatch.setattr(
        coding.store, "get_conversations", lambda ids: {child.id: child, "duplicate": child}
    )
    await coding.evaluate(_request("/workflow retry-dispatch"))
    assert coding.state["status"] == "blocked"
    assert coding.state["pending"] is not None
    assert "reconciled_absent" not in coding.state["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implement", "fix-a", "review-b-standards"])
@pytest.mark.parametrize("command", ["answer", "retry"])
async def test_explicit_retry_exceeds_limit_once_without_advancing_gates(coding, stage, command):
    from omnigent.workflows.controller import question_preview

    await begin(coding)
    state = coding.state
    state.update(current=f"one/{stage}", attempt=1 if stage == "fix-a" else 4, dispatch_serial=3)
    state["fix_attempts"][state["current"]] = 2
    if stage.startswith("fix-"):
        state["findings"] = {"bug": "Missing validation"}
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state["status"] == "blocked"
    question = question_preview(coding.state)
    assert question
    for text in ("Continue", "/workflow answer-question stale Retry"):
        await coding.evaluate(_request(text))
        assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    text = (
        f"/workflow answer-question {question['token']} Access is fixed; retry"
        if command == "answer"
        else "/workflow retry"
    )
    assert (await coding.evaluate(_request(text))).action == PolicyAction.ALLOW
    assert coding.state["status"] == "snapshot"
    assert coding.state["attempt"] == state["attempt"] + 1
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await snapshot(coding)
    dispatched = await coding.dispatch()
    assert dispatched["title"] == f"one/{stage}/4"
    assert coding.state["completed"] == {}
    assert coding.state["verified"] == {}
    assert coding.state["selected"] is None
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
    if stage.endswith("standards"):
        paired = await coding.dispatch()
        assert paired["title"] == "one/review-b-spec/5"
        await coding.evaluate(_completion(paired["title"], outcome="BLOCKED"))
    await coding.evaluate(_completion(dispatched["title"], outcome="BLOCKED"))
    assert coding.state["status"] == ("waiting-user" if stage.endswith("standards") else "blocked")
    assert not coding.state.get("retry_authorization")
    await coding.evaluate(_request("[System: worker wake]"))
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_second_parallel_child_failure_cannot_bypass_reconciliation(coding):
    await begin(coding)
    for _ in range(2):
        await finish(coding)
    standards, spec = await coding.dispatch(), await coding.dispatch()
    await coding.evaluate(_completion(standards["title"]))
    await coding.evaluate(_completion(spec["title"], status="failed", child_id="spec-child"))
    assert coding.state["pending"] is None
    assert coding.state["parallel"]["pending"]["child_id"] == "spec-child"
    for command in ("/workflow retry", "/workflow reject one failed"):
        assert (await coding.evaluate(_request(command))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_candidates_are_sequential_and_only_user_can_reject_blocked_candidate(coding):
    await begin(coding, publish=False, ids=("one", "two"))
    for stage in ("implement", "review-a", "review-b-standards"):
        if not stage.endswith("-b-spec"):
            await finish(coding)
    assert coding.state["current"] == "two/implement"
    assert (await coding.evaluate(_request("/workflow select one"))).action == PolicyAction.DENY
    await finish(coding, outcome="BLOCKED")
    assert coding.state["status"] == "blocked"
    await coding.evaluate(_request("/workflow reject two unsuitable after investigation"))
    assert coding.state["status"] == "waiting-user"
    assert coding.state["verified"] == {"one": "tree-1"}
    assert (await coding.evaluate(_request("/workflow select two"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding)
    assert coding.state["status"] == "complete"


@pytest.mark.asyncio
async def test_worker_enforces_identity_paths_publication_and_review_read_only(
    coding, monkeypatch
):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"], parent_conversation_id=coding.session_id
    )

    async def worker_call(tool, arguments, child_id=child.id):
        engine = build_policy_engine(
            spec=coding.spec, conversation_id=child_id, conversation_store=coding.store
        )
        return await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name=tool,
                content={"name": tool, "arguments": arguments},
            )
        )

    assert (
        await worker_call("sys_os_write", {"path": "/tmp/candidate/file", "text": "yes"})
    ).action == PolicyAction.ALLOW
    for tool, arguments in (
        ("sys_os_write", {"path": "/tmp/other/file"}),
        ("sys_os_read", {"path": "relative.py"}),
        ("sys_os_shell", {"command": "pwd"}),
        ("sys_os_shell", {"command": "git commit -m bypass", "cwd": "/tmp/candidate"}),
        ("workflow_publish", {}),
        ("sys_session_send", {}),
    ):
        assert (await worker_call(tool, arguments)).action == PolicyAction.DENY
    assert (
        await worker_call("sys_os_shell", {"command": "pwd", "cwd": "/tmp/candidate"})
    ).action == PolicyAction.ALLOW
    # Native hooks preserve the harness cwd even when tool_input omits it.
    for command, expected in (
        ("git status --short", PolicyAction.ALLOW),
        ("cd /tmp/candidate && git status --short", PolicyAction.ALLOW),
    ):
        event = hook_payload_to_evaluation_request(
            "PreToolUse",
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": "/tmp/candidate",
            },
        )["event"]
        engine = build_policy_engine(
            spec=coding.spec, conversation_id=child.id, conversation_store=coding.store
        )
        result = await engine.evaluate(
            _build_evaluation_context(Phase.TOOL_CALL, event["data"], event)
        )
        assert result.action == expected
    for patch in (
        "*** Add File: file.py\n+hello",
        "*** Delete File: file.py",
        "*** Update File: file.py\n@@\n-old\n+new",
        "*** Update File: file.py\n*** Move to: renamed.py\n@@\n-old\n+new",
    ):
        assert (
            await worker_call(
                "apply_patch",
                {"input": f"*** Begin Patch\n{patch}\n*** End Patch", "cwd": "/tmp/candidate"},
            )
        ).action == PolicyAction.ALLOW
    for patch in (
        "*** Add File: ../outside.py\n+bad",
        "*** Delete File: /tmp/other/file.py",
        "*** Update File: file.py\n*** Move to: ../outside.py\n@@\n-old\n+new",
        "*** Add File: good.py\n+good\n*** Add File: ../bad.py\n+bad",
        "*** Add File: \n+bad",
    ):
        assert (
            await worker_call("apply_patch", {"input": f"*** Begin Patch\n{patch}\n*** End Patch"})
        ).action == PolicyAction.DENY
    assert (
        await worker_call("apply_patch", {"changes": [{"path": "file.py"}]})
    ).action == PolicyAction.DENY
    assert (await worker_call("apply_patch", {"input": "unknown"})).action == PolicyAction.DENY
    for command in ("cd /tmp/other && pwd", "git worktree add /tmp/other"):
        assert (
            await worker_call("exec_command", {"cmd": command, "workdir": "/tmp/candidate"})
        ).action == PolicyAction.DENY
    assert dispatch["args"]["input"].startswith("/goal ")
    assert len(dispatch["args"]["input"]) < 4000
    assert (await worker_call("workflow_stage", {})).action == PolicyAction.ALLOW
    engine = build_policy_engine(
        spec=coding.spec, conversation_id=child.id, conversation_store=coding.store
    )
    brief = (await engine.evaluate(output("workflow_stage", "stub"))).data
    assert "`cd /tmp/candidate &&`" in brief
    assert "sys_os_write or sys_os_edit" in brief
    await coding.evaluate(_completion(dispatch["title"], child_id=child.id))
    await snapshot(coding)
    await snapshot(coding)
    review = await coding.dispatch()
    reviewer = coding.store.create_conversation(
        title="worker:" + review["title"], parent_conversation_id=coding.session_id
    )
    assert (
        await worker_call("sys_os_write", {"path": "/tmp/candidate/file"}, reviewer.id)
    ).action == PolicyAction.DENY
    assert (
        await worker_call("sys_os_read", {"path": "/tmp/candidate/file"}, reviewer.id)
    ).action == PolicyAction.ALLOW
    assert (
        await worker_call(
            "apply_patch",
            {"input": "*** Begin Patch\n*** Add File: file.py\n+no\n*** End Patch"},
            reviewer.id,
        )
    ).action == PolicyAction.DENY
    assert (
        await worker_call("sys_os_read", {"path": "/tmp/candidate/file"})
    ).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_cancelled_worker_can_retry_after_persisted_idle(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"],
        parent_conversation_id=coding.session_id,
        labels={"omnigent.subagent.dispatch_id": "cancelled-work"},
    )
    coding.store.set_session_live_status(child.id, "idle")
    await coding.evaluate(_completion(dispatch["title"], child_id=child.id, status="cancelled"))
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["pending"] is None
    assert coding.state["status"] == "blocked"
    await coding.evaluate(_request("/workflow retry"))
    await snapshot(coding)
    new = await coding.dispatch()
    assert new["title"] != dispatch["title"]


@pytest.mark.asyncio
async def test_rechecking_selection_preserves_rejected_candidates(coding):
    await begin(coding, publish=False, ids=("one", "two"))
    while coding.state["current"].startswith("one/"):
        await finish(coding)
    await finish(coding, outcome="BLOCKED")
    await coding.evaluate(_request("/workflow reject two unsuitable"))
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding, "changed-after-selection")
    await snapshot(coding, "changed-after-selection")
    for stage in ("review-a", "review-b-standards"):
        if not stage.endswith("-b-spec"):
            await finish(coding, revision="changed-after-selection")
    assert coding.state["status"] == "waiting-user"
    assert coding.state["current"] is None
    assert coding.state["rejected"] == {"two": "unsuitable"}
    assert coding.state["verified"] == {"one": "changed-after-selection"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_findings", [None, "none", [{"id": "broken"}]])
async def test_invalid_parallel_report_cannot_leave_half_accepted_evidence(coding, bad_findings):
    await begin(coding)
    for _ in range(2):
        await finish(coding)
    standards, spec = await coding.dispatch(), await coding.dispatch()
    await coding.evaluate(_completion(standards["title"], child_id="standards-child"))
    ctx = _completion(spec["title"], child_id="spec-child")
    report = json.loads(ctx.content["result"])
    if bad_findings is None:
        report.pop("findings")
    else:
        report["findings"] = bad_findings
    ctx.content["result"] = json.dumps(report)
    await coding.evaluate(ctx)
    await snapshot(coding)
    assert coding.state["status"] == "waiting-user"
    assert "one/review-b-standards" in coding.state["completed"]
    assert "one/review-b-spec" not in coding.state["completed"]
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow answer Repeat the review with valid evidence"))
    await snapshot(coding)
    retry = await coding.dispatch()
    assert "spec" in retry["title"]
    assert "parallel" not in coding.state


@pytest.mark.asyncio
async def test_goal_brief_is_unabridged_and_bound_to_each_parallel_worker(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    for _ in range(2):
        await finish(coding)
    for axis in ("standards", "spec"):
        dispatch = await coding.dispatch()
        child = coding.store.create_conversation(
            title="worker:" + dispatch["title"], parent_conversation_id=coding.session_id
        )
        state = coding.state
        state["completed"]["one/implement"]["evidence"] = "long evidence " * 1000
        coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
        engine = build_policy_engine(
            spec=coding.spec, conversation_id=child.id, conversation_store=coding.store
        )
        brief = (await engine.evaluate(output("workflow_stage", "stub"))).data
        assert f"Stage: one/review-b-{axis}\n" in brief
        assert f'"stage": "one/review-b-{axis}"' in brief
        assert f"Stage: one/review-b-{axis}\n" in dispatch["args"]["input"]
        assert "long evidence " * 1000 in brief
        assert len(dispatch["args"]["input"]) < 4000


@pytest.mark.asyncio
async def test_native_request_echo_cannot_execute_or_repeat_workflow_commands(coding):
    async def native(text):
        event = hook_payload_to_evaluation_request("UserPromptSubmit", {"prompt": text})["event"]
        event["context"]["harness"] = "codex-native"
        before = coding.state
        verdict = await coding.evaluate(
            _build_evaluation_context(Phase.REQUEST, event["data"], event)
        )
        assert verdict.action == PolicyAction.ALLOW
        assert coding.state == before

    await coding.evaluate(_call("workflow_status"))
    definition = {
        "repository": "/tmp/repository",
        "task": "Greeting",
        "baseline": "HEAD",
        "base": "main",
        "candidates": [{"id": "one", "task": "Use a function"}],
    }
    command = "/workflow start " + json.dumps(definition)
    await native(command)
    await coding.evaluate(_request(command))
    await native(command)

    # A fresh probe exercises a user answer and final selection with real stage transitions.
    coding.session_id = coding.store.create_conversation().id
    await begin(coding)
    await finish(coding, outcome="NEEDS_INPUT")
    await native("/workflow answer English")
    await coding.evaluate(_request("/workflow answer English"))
    await native("/workflow answer English")
    await snapshot(coding)
    while coding.state.get("current") is not None:
        await finish(coding)
    await native("/workflow select one")
    await coding.evaluate(_request("/workflow select one"))
    await native("/workflow select one")


@pytest.mark.asyncio
async def test_review_button_binds_review_only_and_requires_one_candidate(coding):
    await coding.evaluate(
        _request(
            "Prepare the start message for `review-fix` using the task below.\nReview my changes"
        )
    )
    definition = {
        "workflow": "implement-review-fix",
        "repository": "/tmp/repository",
        "task": "Review my changes",
        "baseline": "main",
        "base": "main",
        "candidates": [{"id": "current", "task": "Review current implementation"}],
    }
    assert (
        await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    ).action == PolicyAction.DENY
    definition.pop("workflow")
    invalid = {
        **definition,
        "candidates": definition["candidates"] + [{"id": "other", "task": "Other"}],
    }
    assert (
        await coding.evaluate(_request("/workflow start " + json.dumps(invalid)))
    ).action == PolicyAction.DENY
    assert (
        await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    ).action == PolicyAction.ALLOW
    assert coding.state["definition"]["workflow"] == "review-fix"


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["implement-review-fix", "compare-review-fix", "review-fix"])
async def test_idle_controller_drafts_valid_start_without_advancing(coding, workflow):
    from omnigent.workflows.controller import draft_start_message

    await coding.evaluate(
        _request(f"Prepare the start message for `{workflow}` using the task below.\nTask")
    )
    status = json.loads((await coding.evaluate(output("workflow_status", {}))).data)
    assert "workflow_draft" in status["next_action"]
    args = {
        "workflow": workflow,
        "repository": "/tmp/repository",
        "task": "Task",
        "baseline": "main",
        "base": "main",
        "publish": False,
    }
    call = EvaluationContext(
        phase=Phase.TOOL_CALL,
        tool_name="workflow_draft",
        content={"name": "workflow_draft", "arguments": args},
    )
    assert (await coding.evaluate(call)).action == PolicyAction.ALLOW
    draft = json.loads(draft_start_message(**args))
    assert draft["valid"] is True
    await coding.evaluate(output("workflow_draft", draft))
    assert coding.state["status"] == "idle"
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call("sys_session_send"))).action == PolicyAction.DENY
    bad = json.loads(draft_start_message(**{**args, "baseline": ""}))
    assert bad["valid"] is False and "baseline" in bad["error"]
    assert "command" not in bad
    call.content["arguments"]["workflow"] = (
        "review-fix" if workflow != "review-fix" else "implement-review-fix"
    )
    assert (await coding.evaluate(call)).action == PolicyAction.DENY
    assert (await coding.evaluate(_request(draft["command"]))).action == PolicyAction.ALLOW
    assert coding.state["definition"]["publish"] is False
    assert (await coding.evaluate(call)).action == PolicyAction.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["_", "__"])
async def test_prepared_draft_requires_current_explicit_user_selection(coding, separator):
    from omnigent.workflows.controller import draft_start_message

    args = {
        "workflow": "implement-review-fix",
        "repository": "/tmp/repo",
        "task": "Task",
        "baseline": "main",
        "base": "main",
        "branch": "feature/lcs-1670",
    }
    await coding.evaluate(
        _request("Prepare the start message for `implement-review-fix` using the task below.")
    )
    for tool in (
        f"mcp__codex_apps__linear{separator}get_issue",
        f"mcp__codex_apps__linear{separator}get_agent_context",
    ):
        request = hook_payload_to_evaluation_request(
            "PreToolUse", {"tool_name": tool, "tool_input": {"id": "LCS-1670"}}
        )["event"]
        assert (
            await coding.evaluate(
                _build_evaluation_context(Phase.TOOL_CALL, request["data"], request)
            )
        ).action == PolicyAction.ALLOW
    assert (
        await coding.evaluate(_call(f"mcp__codex_apps__linear{separator}delete_issue"))
    ).action == PolicyAction.DENY
    assert (
        await coding.evaluate(_request("/workflow start-draft missing"))
    ).action == PolicyAction.DENY
    draft = json.loads(draft_start_message(**args))
    definition = json.loads(draft["command"].removeprefix("/workflow start "))
    assert definition["candidates"][0]["branch"] == args["branch"]
    await coding.evaluate(output("workflow_draft", draft))
    token = coding.state["draft"]["token"]
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY
    await coding.evaluate(_request("Change the task"))
    assert "draft" not in coding.state
    assert (
        await coding.evaluate(_request("/workflow start-draft " + token))
    ).action == PolicyAction.DENY
    await coding.evaluate(output("workflow_draft", draft))
    token = coding.state["draft"]["token"]
    await coding.evaluate(_request(""))
    assert coding.state["draft"]["token"] == token
    # The fixture reconstructs the policy from SQLite on each call.
    native = _request("/workflow start-draft " + token)
    native = EvaluationContext(phase=Phase.REQUEST, content=native.content, harness="codex-native")
    await coding.evaluate(native)
    assert coding.state["status"] == "idle"
    assert (
        await coding.evaluate(_request("/workflow start-draft " + token))
    ).action == PolicyAction.ALLOW
    assert coding.state["definition"]["candidates"][0]["branch"] == args["branch"]
    assert coding.state["definition"]["publish"] is False
    assert (
        await coding.evaluate(_request("/workflow start-draft " + token))
    ).action == PolicyAction.DENY
    assert (
        await coding.evaluate(_call(f"mcp__codex_apps__linear{separator}get_issue"))
    ).action == PolicyAction.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_command", ["/workflow retry", "/workflow answer Retry with correct evidence"]
)
async def test_malformed_stage_preserves_worker_question_without_advancing(coding, retry_command):
    await begin(coding)
    dispatch = await coding.dispatch()
    report = {
        "stage": "wrong-stage",
        "outcome": "NEEDS_INPUT",
        "evidence": "The current cache cannot establish deletion.",
        "question": "May I use a read-only Registry batch query?",
        "findings": [],
        "resolved": [],
    }
    await coding.evaluate(
        output(
            "sys_session_send",
            report,
            {
                "name": "sys_session_send",
                "args": {
                    "agent": "worker",
                    "title": dispatch["title"],
                    "conversation_id": "child-1",
                },
                "subagent": {"status": "completed", "work_id": "work-1"},
            },
        )
    )
    assert coding.state["status"] == "blocked"
    assert report["question"] in coding.state["question"]
    assert coding.state["incomplete"]["one/implement"]["evidence"] == report["evidence"]
    assert coding.state["completed"] == {}
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_request("Unrelated message"))
    assert coding.state["status"] == "blocked"
    await coding.evaluate(_request(retry_command))
    await snapshot(coding)
    retry = await coding.dispatch()
    assert retry["title"].startswith("one/implement/")
    assert report["question"] in coding.state["incomplete"]["one/implement"]["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["workflow_reconcile", "workflow_status"])
@pytest.mark.parametrize("workflow", ["implement-review-fix", "review-fix"])
async def test_recover_native_question_and_bind_answer_across_restart(
    coding, monkeypatch, source, workflow
):
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.workflows.controller import draft_preview

    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding, workflow=workflow)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"],
        parent_conversation_id=coding.session_id,
        labels={
            "omnigent.subagent.dispatch_id": "saved-work",
            "omnigent.wrapper": "codex-native-ui",
        },
    )
    report = {
        "stage": dispatch["title"].split("/")[-2],
        "outcome": "NEEDS_INPUT",
        "evidence": "Inspected Registry integration",
        "question": "May I use Registry directly?",
    }
    coding.store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="native-turn",
                data=MessageData(
                    role="assistant",
                    agent="worker",
                    content=[{"type": "output_text", "text": json.dumps(report)}],
                ),
            )
        ],
    )
    coding.store.set_session_live_status(child.id, "running")
    await coding.evaluate(output(source, {}))
    assert coding.state["status"] == "running"
    coding.store.set_session_live_status(child.id, "idle")
    await coding.evaluate(output(source, {}))
    assert coding.state["status"] == "waiting-user"
    assert report["question"] in coding.state["question"]
    assert coding.state["pending"] is None
    assert coding.state["completed"] == {}
    assert coding.state["last_result"]["work_id"] == "saved-work"
    token = draft_preview({STATE_KEY: coding.state})["question"]["token"]
    # Each evaluate rebuilds the engine from SQLite, including duplicate recovery.
    await coding.evaluate(output(source, {}))
    await coding.evaluate(_request("Unrelated message"))
    assert coding.state["status"] == "waiting-user"
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    for command in ["/workflow answer-question stale Yes", f"/workflow answer-question {token} "]:
        assert (await coding.evaluate(_request(command))).action == PolicyAction.DENY
    assert (
        await coding.evaluate(_request(f"/workflow answer-question {token} Use Registry"))
    ).action == PolicyAction.ALLOW
    assert coding.state["answer"] == "Use Registry"
    assert coding.state["status"] == "snapshot"
    assert (
        await coding.evaluate(_request(f"/workflow answer-question {token} Duplicate"))
    ).action == PolicyAction.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["interrupted", "success", "wrong-stage", "no-dispatch", "newer-input"]
)
async def test_recovery_does_not_infer_question_from_untrusted_or_incomplete_history(
    coding, monkeypatch, variant
):
    from omnigent.entities import MessageData, NewConversationItem

    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    dispatch = await coding.dispatch()
    labels = {"omnigent.wrapper": "codex-native-ui"}
    if variant != "no-dispatch":
        labels["omnigent.subagent.dispatch_id"] = "saved-work"
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"],
        parent_conversation_id=coding.session_id,
        labels=labels,
    )
    coding.store.set_session_live_status(child.id, "idle")
    report = {
        "stage": "wrong" if variant == "wrong-stage" else "implement",
        "outcome": "COMPLETE" if variant == "success" else "NEEDS_INPUT",
        "evidence": "Inspected",
        "question": "Which source?",
    }
    coding.store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="native-turn",
                data=MessageData(
                    role="assistant",
                    agent="worker",
                    interrupted=variant == "interrupted",
                    content=[{"type": "output_text", "text": json.dumps(report)}],
                ),
            )
        ],
    )
    if variant == "newer-input":
        coding.store.append(
            child.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="next-turn",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "Continue"}]
                    ),
                )
            ],
        )
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["status"] == "running"
    assert coding.state["pending"]
    assert coding.state["completed"] == {}


@pytest.mark.asyncio
async def test_answer_reappears_when_next_worker_reports_blocked(coding, monkeypatch):
    from omnigent.workflows.controller import draft_preview

    await begin(coding, publish=False)
    await finish(coding, outcome="NEEDS_INPUT")
    first = draft_preview({STATE_KEY: coding.state})["question"]
    await coding.evaluate(
        _request(f"/workflow answer-question {first['token']} Keep one worktree")
    )
    await snapshot(coding)
    await finish(coding, outcome="BLOCKED")
    assert coding.state["status"] == "blocked"
    second = draft_preview({STATE_KEY: coding.state})["question"]
    assert second is not None, "A blocked worker question must expose the Answer action"
    assert second["token"] != first["token"]
    stopped = coding.state
    await coding.evaluate(_request("Unrelated message"))
    await coding.evaluate(output("workflow_status", {}))
    assert coding.state == stopped
    for command in (
        f"/workflow answer-question {first['token']} Old answer",
        f"/workflow answer-question {second['token']} ",
        "/workflow select one",
    ):
        assert (await coding.evaluate(_request(command))).action == PolicyAction.DENY
    for tool in ("sys_session_send", "workflow_publish"):
        assert (await coding.evaluate(_call(tool))).action == PolicyAction.DENY
    answer = f"/workflow answer-question {second['token']} Retry after access is fixed"
    assert (await coding.evaluate(_request(answer))).action == PolicyAction.ALLOW
    assert coding.state["status"] == "snapshot"
    assert coding.state["current"] == "one/implement"
    assert coding.state["answer"] == "Retry after access is fixed"
    assert coding.state["completed"] == {}
    assert coding.state["verified"] == {}
    assert coding.state["selected"] is None
    assert coding.state["definition"]["publish"] is False
    assert (await coding.evaluate(_request(answer))).action == PolicyAction.DENY
    await snapshot(coding)
    retry = await coding.dispatch()
    assert retry["title"] == "one/implement/3"
    child = coding.store.create_conversation(
        parent_conversation_id=coding.session_id, title="worker:" + retry["title"]
    )
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    brief = _worker_policy(
        {"type": "tool_result", "target": "workflow_stage"}, coding.session_id, child.id
    )
    assert "Retry after access is fixed" in brief["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"pending": {"title": "uncertain", "stage": "one/implement"}},
        {"parallel": {"pending": {"title": "uncertain", "stage": "one/review-b-spec"}}},
        {"current": None},
        {"selected": "one"},
        {"status": "waiting-design-selection"},
        {"status": "waiting-design-revision"},
        {"status": "waiting-final-selection"},
        {"status": "cancelled"},
    ],
)
async def test_stage_answer_does_not_replace_other_gates(coding, overrides):
    from omnigent.workflows.controller import draft_preview

    await begin(coding, publish=False)
    await finish(coding, outcome="BLOCKED")
    state = {**coding.state, **overrides}
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    assert draft_preview({STATE_KEY: state})["question"] is None
    for command in ("/workflow answer Retry", "/workflow answer-question stale Retry"):
        result = await coding.evaluate(_request(command))
        assert result.action == (
            PolicyAction.ALLOW if state["status"] == "cancelled" else PolicyAction.DENY
        )
        assert coding.state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [*STAGES, "explore", "compare-results"])
async def test_stage_model_and_effort_are_enforced_only_for_new_workers(coding, stage):
    await begin(coding)
    session = coding.store.get_conversation(coding.session_id)
    state = session.session_state[STATE_KEY]
    state.update(
        current=stage if stage in {"explore", "compare-results"} else "one/" + stage,
        status="ready",
    )
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    if stage.startswith("fix-"):
        state["findings"] = {"bug": "Missing validation"}
    if stage.startswith("verify-"):
        state["rechecks"] = [stage]
        if stage.endswith("standards"):
            state["rechecks"].append("verify-b-spec")
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    dispatch = await coding.dispatch()
    implementation = stage == "implement" or stage.startswith("fix-")
    assert dispatch["args"]["model"] == ("gpt-6.1-sol" if implementation else "gpt-6-astra")
    assert dispatch["args"]["reasoning_effort"] == (
        "high" if stage.startswith("fix-") else "xhigh"
    )
    if stage.endswith("-b-standards"):
        paired = await coding.dispatch()
        assert paired["args"]["model"] == "gpt-6-astra"
        assert paired["args"]["reasoning_effort"] == "xhigh"
    before = coding.state
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state == before
    assert coding.spec.executor.model == "gpt-6.1-sol"
    assert coding.spec.executor.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_draft_stage_settings_survive_approval_and_policy_reload(coding):
    from omnigent.workflows.controller import draft_start_message

    args = {
        "workflow": "compare-review-fix",
        "repository": "/tmp/repository",
        "task": "Compare",
        "baseline": "HEAD",
        "base": "main",
        "worker_defaults": {"reasoning_effort": "medium"},
        "stages": {"explore": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}},
    }
    result = await coding.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="workflow_draft",
            content={"name": "workflow_draft", "arguments": args},
        )
    )
    draft = json.loads(draft_start_message(**result.data))
    await coding.evaluate(output("workflow_draft", draft))
    saved = coding.state["draft"]
    # A changed bundle must not alter the approved draft or a running workflow.
    coding.spec.guardrails.policies[0].function.arguments["worker_defaults"] = {
        "model": "gpt-6.1-sol",
        "reasoning_effort": "low",
    }
    coding.spec.guardrails.policies[0].function.arguments["stages"]["review-a"] = {
        "model": "gpt-6.1-sol",
        "reasoning_effort": "low",
    }
    await coding.evaluate(_request("/workflow start-draft " + saved["token"]))
    assert coding.state["definition"] == saved["definition"]
    dispatch = await coding.dispatch()
    assert dispatch["args"]["model"] == "gpt-6.1-sol"
    assert dispatch["args"]["reasoning_effort"] == "high"
    assert coding.state["definition"]["worker_defaults"] == {
        "model": "gpt-6-astra",
        "reasoning_effort": "medium",
    }
    assert coding.state["definition"]["stages"]["fix-a"] == {
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"worker_defaults": []},
        {"worker_defaults": None},
        {"worker_defaults": {"model": "--bad"}},
        {"worker_defaults": {"model": 42}},
        {"worker_defaults": {"reasoning_effort": "extreme"}},
        {"worker_defaults": {"reasoning_effort": []}},
        {"stages": {"review-typo": {"reasoning_effort": "high"}}},
        {"stages": {"review-a": {"extra": "setting"}}},
        {"stages": {"implement": None}},
        {"stages": []},
    ],
)
async def test_invalid_stage_settings_cannot_start_work(coding, settings):
    await coding.evaluate(output("workflow_status", {}))
    definition = dict(
        workflow="compare-review-fix",
        repository="/tmp/repository",
        task="Compare",
        baseline="HEAD",
        base="main",
        **settings,
    )
    result = await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    assert result.action == PolicyAction.DENY
    assert coding.state.get("status", "idle") == "idle"


@pytest.mark.asyncio
async def test_older_bundle_draft_tool_receives_no_new_arguments(coding):
    coding.spec.guardrails.policies[0].function.arguments.clear()
    result = await coding.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="workflow_draft",
            content={
                "name": "workflow_draft",
                "arguments": {
                    "workflow": "implement-review-fix",
                    "repository": "/tmp/repository",
                    "task": "Task",
                    "baseline": "HEAD",
                    "base": "main",
                },
            },
        )
    )
    assert result.action == PolicyAction.ALLOW
    assert result.data is None
