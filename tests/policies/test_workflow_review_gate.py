"""Reviews route routine defects to fixes and preserve real user decisions."""

import pytest

from omnigent.spec.types import PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.controller import STAGES, _brief
from tests.policies.test_workflow_comparison import explore, prepare, present, report, start
from tests.policies.test_workflow_controller import (
    _call,
    _completion,
    _request,
    begin,
    finish,
    output,
    snapshot,
)
from tests.policies.test_workflow_controller import (
    coding as coding,
)

REVIEWS = [stage for stage in STAGES if stage.startswith(("review-", "verify-", "final-"))]


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["implement-review-fix", "compare-review-fix", "review-fix"])
@pytest.mark.parametrize("stage", REVIEWS)
@pytest.mark.parametrize("outcome", ["COMPLETE", "NEEDS_INPUT", "BLOCKED"])
async def test_every_review_waits_for_explicit_decision(coding, db_uri, workflow, stage, outcome):
    if workflow == "compare-review-fix":
        await start(coding)
        await explore(coding, ("simple",))
        await present(coding)
        await coding.evaluate(_request("/workflow designs 1 all"))
        await prepare(coding)
    else:
        await begin(coding, workflow=workflow)
    first = stage.removesuffix("spec") + "standards" if stage.endswith("-b-spec") else stage
    while not coding.state["current"].endswith("/" + first):
        await finish(coding)
    dispatches = [await coding.dispatch()]
    if coding.state.get("parallel"):
        dispatches.append(await coding.dispatch())
    for dispatch in reversed(dispatches):
        extra = (
            {
                "outcome": outcome,
                "findings": [
                    {"id": "conflict", "message": "Two requirements conflict"},
                    {"id": "validation", "message": "Reject invalid input"},
                ],
                "question": "Which requirement takes priority?",
            }
            if dispatch["title"].rsplit("/", 1)[0].endswith("/" + stage)
            else {}
        )
        await report(coding, dispatch, extra)
    if coding.state["status"] == "snapshot":
        await snapshot(coding)
    assert coding.state["status"] == "waiting-user"
    assert "Two requirements conflict" in coding.state["question"]
    assert "Which requirement takes priority?" in coding.state["question"]
    saved = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    for message in (
        "continue",
        "What is the status?",
        "/workflow retry",
        "/workflow select simple",
    ):
        await coding.evaluate(_request(message))
        assert coding.state == saved
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state == saved
    for tool in ("sys_session_send", "workflow_snapshot", "workflow_prepare", "workflow_publish"):
        assert (await coding.evaluate(_call(tool))).action == PolicyAction.DENY
    assert (
        await coding.evaluate(_request("/workflow answer Preserve compatibility"))
    ).action == PolicyAction.ALLOW
    await snapshot(coding)
    resumed = await coding.dispatch()
    expected = (
        ("fix-b" if "-b-" in stage else "fix-" + stage[-1]) if outcome == "COMPLETE" else first
    )
    assert resumed["title"].rsplit("/", 1)[0].endswith("/" + expected)
    assert coding.state["answer"] == "Preserve compatibility"
    assert coding.state["decisions"][-1]["command"] == "/workflow answer Preserve compatibility"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_parallel_review_keeps_both_questions(coding, reverse):
    await begin(coding)
    for _ in range(4):
        await finish(coding)
    dispatches = [await coding.dispatch(), await coding.dispatch()]
    if reverse:
        dispatches.reverse()
    for dispatch in dispatches:
        axis = dispatch["title"].rsplit("/", 1)[0].rsplit("-", 1)[1]
        await report(coding, dispatch, {"outcome": "NEEDS_INPUT", "question": f"Resolve {axis}?"})
    assert coding.state["status"] == "waiting-user"
    assert "Resolve standards?" in coding.state["question"]
    assert "Resolve spec?" in coding.state["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["COMPLETE", "NEEDS_INPUT", "BLOCKED"])
@pytest.mark.parametrize("failure_kind", ["failed", "absent"])
async def test_recovery_keeps_review_decision(coding, db_uri, monkeypatch, outcome, failure_kind):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding)
    for _ in range(4):
        await finish(coding)
    standards, spec = await coding.dispatch(), await coding.dispatch()
    await report(
        coding,
        standards,
        {
            "outcome": outcome,
            "findings": [{"id": "conflict", "message": "Resolve conflict"}],
            "question": "Which behavior?",
        },
    )
    if failure_kind == "failed":
        child = coding.store.create_conversation(
            title="worker:" + spec["title"],
            parent_conversation_id=coding.session_id,
            labels={"omnigent.subagent.dispatch_id": "failed-work"},
        )
        coding.store.set_session_live_status(child.id, "failed")
        await coding.evaluate(_completion(spec["title"], child_id=child.id, status="failed"))
    else:
        await coding.evaluate(
            output("sys_session_send", "Error: dispatch lost", {"args": {"title": spec["title"]}})
        )
    coding.store = SqlAlchemyConversationStore(db_uri)
    await coding.evaluate(output("workflow_reconcile", {}))
    if failure_kind == "absent":
        await coding.evaluate(_request("/workflow retry-dispatch"))
    assert coding.state["status"] == "waiting-user"
    assert "Which behavior?" in coding.state["question"]
    assert "Resolve conflict" in coding.state["question"]
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow answer Keep current behavior"))
    await snapshot(coding)
    assert "review-b-standards" in (await coding.dispatch())["title"]


@pytest.mark.asyncio
async def test_changed_revision_after_review_answer_requires_fresh_review(coding):
    await begin(coding)
    await finish(coding)
    await finish(
        coding,
        findings=[{"id": "bug", "message": "Missing validation"}],
        question="Which input format? The requirements conflict.",
    )
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Fix validation"))
    await snapshot(coding, revision="changed-during-wait")
    assert "review-a" in (await coding.dispatch())["title"]
    assert coding.state["verified"] == {}


@pytest.mark.asyncio
async def test_reject_candidate_at_review_gate_does_not_fix_it(coding):
    await begin(coding, ids=("one", "two"))
    await finish(coding)
    await finish(
        coding,
        findings=[{"id": "risk", "message": "Unacceptable risk"}],
        question="Accept this risk? It exceeds the approved scope.",
    )
    assert (
        await coding.evaluate(_request("/workflow reject one Too risky"))
    ).action == PolicyAction.ALLOW
    assert coding.state["current"] == "two/implement"
    assert coding.state["rejected"] == {"one": "Too risky"}
    assert "review_wait" not in coding.state


@pytest.mark.asyncio
async def test_design_revision_clears_review_resume_target(coding):
    await start(coding)
    await explore(coding, ("simple",))
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 all"))
    await prepare(coding)
    await finish(coding)
    await finish(
        coding,
        findings=[{"id": "risk", "message": "Revise the design"}],
        question="Revise this design? The approved approach cannot satisfy the requirement.",
    )
    assert coding.state["review_wait"]["next_stage"] == "fix-a"
    await coding.evaluate(_request("/workflow designs 1 revise Avoid this risk"))
    assert "review_wait" not in coding.state
    assert coding.state["design_history"][-1]["review_wait"]["next_stage"] == "fix-a"
    dispatch = await coding.dispatch()
    await report(coding, dispatch, {"outcome": "NEEDS_INPUT", "question": "Which constraint?"})
    await coding.evaluate(_request("/workflow answer Avoid new dependencies"))
    assert coding.state["status"] == "ready" and coding.state["current"] == "explore"
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["implement-review-fix", "compare-review-fix", "review-fix"])
@pytest.mark.parametrize("stage", REVIEWS)
async def test_every_review_routes_routine_defects_to_fix(coding, db_uri, workflow, stage):
    if workflow == "compare-review-fix":
        await start(coding)
        await explore(coding, ("simple",))
        await present(coding)
        await coding.evaluate(_request("/workflow designs 1 all"))
        await prepare(coding)
    else:
        await begin(coding, workflow=workflow)
    first = stage.removesuffix("spec") + "standards" if stage.endswith("-b-spec") else stage
    while not coding.state["current"].endswith("/" + first):
        await finish(coding)
    dispatches = [await coding.dispatch()]
    if coding.state.get("parallel"):
        dispatches.append(await coding.dispatch())
    for dispatch in reversed(dispatches):
        extra = (
            {"findings": [{"id": "validation", "message": "Reject invalid input"}]}
            if dispatch["title"].rsplit("/", 1)[0].endswith("/" + stage)
            else {}
        )
        await report(coding, dispatch, extra)
    await snapshot(coding)
    fix = "fix-b" if "-b-" in stage else "fix-" + stage[-1]
    assert coding.state["status"] == "snapshot"
    assert coding.state["current"].endswith("/" + fix)
    assert coding.state.get("question") is None
    assert "review_wait" not in coding.state
    findings = coding.state["findings"]
    assert list(findings.values()) == ["Reject invalid input"]
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state["findings"] == findings
    assert coding.state["verified"] == {}
    assert (await coding.evaluate(_request("/workflow select one"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
    await snapshot(coding)
    dispatch = await coding.dispatch()
    assert "Reject invalid input" in _brief(coding.state)
    await report(coding, dispatch, {})  # A claimed completion cannot skip the defect.
    await snapshot(coding)
    assert coding.state["status"] == "blocked"
    assert "every outstanding finding" in coding.state["question"]
    assert coding.state["verified"] == {}


@pytest.mark.asyncio
async def test_decision_history_preserves_accepted_exception_after_later_answer(coding, db_uri):
    await begin(coding)
    await finish(coding)
    await finish(
        coding,
        findings=[{"id": "legacy", "message": "Existing legacy mismatch"}],
        question="May this existing mismatch remain? Fixing it changes an unrelated API.",
    )
    await coding.evaluate(_request("/workflow answer Accept only legacy as an exception"))
    await snapshot(coding)
    await finish(coding)
    await finish(
        coding,
        findings=[{"id": "validation", "message": "Reject invalid input"}],
        question="Which format is required? The two specifications conflict.",
    )
    saved = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == saved
    await coding.evaluate(_request("/workflow answer Use the existing input format"))
    await snapshot(coding)
    await coding.dispatch()
    assert "Accept only legacy as an exception" in _brief(coding.state)
    assert "Use the existing input format" in _brief(coding.state)
    assert coding.state["findings"] == {"validation": "Reject invalid input"}
    assert len(coding.state["decisions"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_parallel_mixed_reports_preserve_routine_defect_and_decision(
    coding, db_uri, reverse
):
    await begin(coding)
    for _ in range(4):
        await finish(coding)
    dispatches = [await coding.dispatch(), await coding.dispatch()]
    if reverse:
        dispatches.reverse()
    for dispatch in dispatches:
        extra = (
            {"findings": [{"id": "validation", "message": "Reject invalid input"}]}
            if "standards" in dispatch["title"]
            else {
                "outcome": "NEEDS_INPUT",
                "question": "Which input format? Requirements conflict.",
            }
        )
        await report(coding, dispatch, extra)
    saved = coding.state
    assert saved["status"] == "waiting-user"
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == saved
    await coding.evaluate(_request("/workflow answer Use the existing format"))
    reports = coding.state["decisions"][-1]["review"]["reports"]
    assert reports["one/review-b-standards"]["findings"][0]["id"] == "validation"
    assert reports["one/review-b-spec"]["question"] == "Which input format? Requirements conflict."
    await snapshot(coding)
    assert coding.state["current"] == "one/review-b-standards"
    assert "Reject invalid input" in _brief(coding.state)
