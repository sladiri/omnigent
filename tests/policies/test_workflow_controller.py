"""Workflow gates through real policies and SQLite, rebuilt after every event."""

from __future__ import annotations

import json
from pathlib import Path
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
from omnigent.workflows.controller import STAGES, STATE_KEY

CONFIG = Path(__file__).parents[2] / "examples/workflow_controller"


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
    question: str = "Which language should the greeting use?",
) -> EvaluationContext:
    output = json.dumps(
        {
            "stage": title.rsplit("/", 1)[0],
            "outcome": outcome,
            "evidence": evidence,
            "findings": [],
            "resolved": [],
            "question": question,
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


async def snapshot(probe, revision="tree-1"):
    await probe.evaluate(
        output("workflow_snapshot", {"revision": revision, "branch": "candidate"})
    )


async def finish(probe, findings=None, revision="tree-1", outcome="COMPLETE"):
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
        "stage": probe.state["current"],
        "outcome": outcome,
        "evidence": "Checks passed",
        "findings": findings or [],
        "resolved": list(probe.state["findings"]),
        "question": "Which language?",
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
async def test_full_review_sequence_selection_and_publication_gate(coding, workflow):
    await begin(coding, workflow=workflow)
    titles = []
    stages = STAGES[1:] if workflow == "review-fix" else STAGES
    for stage in stages:
        if stage.endswith("-b-spec"):
            continue
        assert coding.state["current"] == "one/" + stage
        assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
        request = await finish(coding)
        titles.append(request["args"]["title"])
    assert len(set(titles)) == (10 if workflow == "review-fix" else 11)
    assert set(coding.state["completed"]) == {"one/" + stage for stage in stages}
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("worker suggests /workflow select one"))
    assert coding.state["selected"] is None
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding)
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


@pytest.mark.asyncio
async def test_findings_require_resolution_and_failed_recheck_loops_to_fix(coding):
    await begin(coding, publish=False)
    await finish(coding)
    await finish(coding, findings=[{"id": "A-1", "message": "Missing case"}])
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Fix the reported case"))
    await snapshot(coding)
    assert coding.state["current"] == "one/fix-a"
    await finish(coding)
    await finish(coding, findings=[{"id": "A-2", "message": "Still missing"}])
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Fix the reported case"))
    await snapshot(coding)
    assert coding.state["current"] == "one/fix-a"
    await finish(coding)
    await finish(coding, findings=[{"id": "A-3", "message": "Still missing"}])
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Fix the reported case"))
    await snapshot(coding)
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state["status"] == "blocked"
    assert "limit" in coding.state["question"]


@pytest.mark.asyncio
async def test_final_b_findings_rerun_b_and_c_on_the_fixed_revision(coding):
    await begin(coding)
    revision = "tree-1"
    while coding.state["current"] != "one/final-b-standards":
        if coding.state["current"] == "one/fix-c":
            revision = "tree-after-c"
        await finish(coding, revision=revision)
    await finish(
        coding, findings=[{"id": "regression", "message": "C broke B"}], revision=revision
    )
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Fix the regression"))
    await snapshot(coding, revision)
    assert coding.state["current"] == "one/fix-b"
    assert coding.state["verified"] == {}
    revision = "tree-fixed"
    for expected in (
        "fix-b",
        "verify-b-standards",
        "review-c",
        "fix-c",
        "verify-c",
        "final-b-standards",
    ):
        assert coding.state["current"] == "one/" + expected
        await finish(coding, revision=revision)
    assert coding.state["verified"] == {"one": revision}
    for stage in ("verify-c", "final-b-standards", "final-b-spec"):
        assert coding.state["completed"]["one/" + stage]["revision"] == revision


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
async def test_selection_invalidates_changed_candidate(coding):
    await begin(coding)
    for _ in STAGES:
        if _.endswith("-b-spec"):
            continue
        await finish(coding)
    await coding.evaluate(_request("/workflow select one"))
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
    for _ in range(4):
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
    assert coding.state["current"] == "one/fix-b"
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
async def test_attempt_limit_denies_dispatch_and_persists_blocked_state(coding):
    await begin(coding)
    for _ in range(3):
        await finish(coding, outcome="NEEDS_INPUT")
        await coding.evaluate(_request("/workflow answer clarified"))
        await snapshot(coding)
    result = await coding.evaluate(_call())
    assert result.action == PolicyAction.DENY
    assert coding.state["status"] == "blocked"
    assert coding.state["pending"] is None
    assert coding.state["dispatch_serial"] == 3


@pytest.mark.asyncio
async def test_second_parallel_child_failure_cannot_bypass_reconciliation(coding):
    await begin(coding)
    for _ in range(4):
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
    for stage in STAGES:
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
    # Codex hooks carry only the command; workdir is not part of tool_input.
    for command, expected in (
        ("git status --short", PolicyAction.DENY),
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
    assert dispatch["args"].startswith("/goal ")
    assert len(dispatch["args"]) < 4000
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
    for stage in STAGES[1:]:
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
    for _ in range(4):
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
    assert not any(key.startswith("one/review-b-") for key in coding.state["completed"])
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow answer Repeat the review with valid evidence"))
    await snapshot(coding)
    retry = await coding.dispatch()
    assert "standards" in retry["title"]
    assert coding.state["parallel"]["next"] == "one/review-b-spec"


@pytest.mark.asyncio
async def test_goal_brief_is_unabridged_and_bound_to_each_parallel_worker(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    for _ in range(4):
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
        assert "long evidence " * 1000 in brief
        assert len(dispatch["args"]) < 4000


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
