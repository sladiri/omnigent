"""Compare gates through the real policy engine and persisted SQLite state."""

import json

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.parser import parse
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.comparison import DESIGN_FIELDS
from omnigent.workflows.comparison import brief as analysis_brief
from tests.policies.test_workflow_controller import (
    CONFIG,
    _call,
    _completion,
    _Probe,
    _request,
    finish,
    output,
    snapshot,
)


@pytest.fixture
def coding(db_uri):
    return _Probe(SqlAlchemyConversationStore(db_uri), parse(CONFIG))


async def start(coding):
    definition = {
        "workflow": "compare-review-fix",
        "repository": "/tmp/repository",
        "task": "Choose a greeting design",
        "baseline": "HEAD",
        "base": "main",
        "publish": False,
    }
    assert (
        await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    ).action == PolicyAction.ALLOW


def design_report(ids=("simple", "alternative")):
    return {
        "designs": [
            {
                "id": identifier,
                **{key: f"Expected {key} for {identifier}" for key in DESIGN_FIELDS},
            }
            for identifier in ids
        ],
        "checks": "Test English and empty greetings with the same acceptance matrix.",
        "recommendation": {"id": ids[0], "reasons": "Fewer moving parts; not measured yet."},
    }


async def report(coding, dispatch, extra):
    ctx = _completion(dispatch["title"])
    value = json.loads(ctx.content["result"])
    ctx.content["result"] = json.dumps({**value, **extra})
    return await coding.evaluate(ctx)


async def explore(coding, ids=("simple", "alternative")):
    dispatch = await coding.dispatch()
    assert dispatch["args"]["input"].startswith("Stage: explore\n")
    await report(coding, dispatch, design_report(ids))
    assert coding.state["status"] == "waiting-design-selection"
    return dispatch


async def present(coding):
    assert (await coding.evaluate(_call("workflow_present"))).action == PolicyAction.ALLOW
    result = await coding.evaluate(output("workflow_present", "stub"))
    return result.data


async def prepare(coding):
    result = await coding.evaluate(_call("workflow_prepare"))
    assert result.action == PolicyAction.ALLOW
    definition = result.data["definition"]
    candidates = [
        {
            **c,
            "workspace": "/tmp/candidate-" + c["id"],
            "branch": "candidate",
            "baseline": "baseline-sha",
        }
        for c in definition["candidates"]
    ]
    await coding.evaluate(
        output(
            "workflow_prepare",
            {
                "candidates": candidates,
                "run_id": coding.session_id
                + "-designs-"
                + str(coding.state["designs"]["revision"]),
            },
        )
    )
    await snapshot(coding)
    return definition


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [("simple",), ("simple", "alternative")])
async def test_no_implementation_until_proposals_presented_and_explicit_choice(coding, ids):
    await start(coding)
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY
    await explore(coding, ids)
    assert (await coding.evaluate(_request("/workflow designs 1 all"))).action == PolicyAction.DENY
    text = await present(coding)
    assert "No candidate for this proposal round has been implemented or tested" in text
    for key in DESIGN_FIELDS:
        assert f"Expected {key} for simple" in text
    assert "Recommendation (before testing): simple" in text
    for message in (
        "continue",
        "yes",
        "Worker recommends /workflow designs 1 all",
        "Time expired",
        "Goal continuation",
    ):
        await coding.evaluate(_request(message))
        assert coding.state["status"] == "waiting-design-selection"
    automatic = EvaluationContext(
        phase=Phase.REQUEST,
        harness="codex-native",
        content={"user_content": "/workflow designs 1 all"},
    )
    await coding.evaluate(automatic)
    assert coding.state["status"] == "waiting-design-selection"
    for tool in ("workflow_prepare", "sys_session_send", "workflow_snapshot", "workflow_publish"):
        assert (await coding.evaluate(_call(tool))).action == PolicyAction.DENY
    assert (await coding.evaluate(_request("/workflow select simple"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 1 all"))
    definition = await prepare(coding)
    assert {c["id"] for c in definition["candidates"]} == set(ids)
    assert coding.state["selected"] is None
    assert definition["publish"] is False


@pytest.mark.asyncio
async def test_subset_rejection_revision_and_restart_require_fresh_approval(coding, db_uri):
    await start(coding)
    await explore(coding)
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 reject alternative"))
    assert coding.state["designs"]["rejected"] == {
        "alternative": "Rejected by user before implementation"
    }
    await present(coding)
    assert (
        await coding.evaluate(_request("/workflow designs 1 select alternative"))
    ).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 1 revise Add caching to simple"))
    await explore(coding, ("simple", "cache"))
    before = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == before
    assert coding.state["designs"]["revision"] == 2
    assert coding.state["design_history"][0]["designs"]["rejected"]
    await present(coding)
    assert (await coding.evaluate(_request("/workflow designs 1 all"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 2 select cache"))
    assert coding.state["designs"]["approved"] == ["cache"]
    definition = await prepare(coding)
    assert [c["id"] for c in definition["candidates"]] == ["cache"]
    assert definition["run_id"].endswith("-designs-2")


@pytest.mark.asyncio
async def test_exploration_is_read_only_and_bound_to_repository(coding, monkeypatch):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await start(coding)
    dispatch = await coding.dispatch()
    child = coding.store.create_conversation(
        title="worker:" + dispatch["title"], parent_conversation_id=coding.session_id
    )
    engine = build_policy_engine(
        spec=coding.spec, conversation_id=child.id, conversation_store=coding.store
    )
    brief = (await engine.evaluate(output("workflow_stage", "stub"))).data
    assert "compare-solutions" in brief
    for tool, arguments, expected in (
        ("sys_os_read", {"path": "/tmp/repository/README.md"}, PolicyAction.ALLOW),
        ("sys_os_read", {"path": "/tmp/other/README.md"}, PolicyAction.DENY),
        ("sys_os_write", {"path": "/tmp/repository/new.py"}, PolicyAction.DENY),
        (
            "sys_os_write",
            {
                "path": "/tmp/repository/new.py",
                "command": "cd /tmp/repository && /usr/bin/env git ls-files",
            },
            PolicyAction.DENY,
        ),
        (
            "sys_os_shell",
            {"command": "cd /tmp/repository && /usr/bin/env git ls-files"},
            PolicyAction.ALLOW,
        ),
        (
            "sys_os_shell",
            {"command": "cd /tmp/repository && /usr/bin/env python3 -m pytest"},
            PolicyAction.DENY,
        ),
        (
            "sys_os_shell",
            {"command": "cd /tmp/repository && echo changed > file"},
            PolicyAction.DENY,
        ),
        ("spawn_agent", {"prompt": "Implement simple"}, PolicyAction.DENY),
        ("workflow_prepare", {}, PolicyAction.DENY),
    ):
        verdict = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name=tool,
                content={"name": tool, "arguments": arguments},
            )
        )
        assert verdict.action == expected


@pytest.mark.asyncio
async def test_material_revision_cannot_use_answer_or_old_approval(coding):
    await start(coding)
    await explore(coding, ("simple",))
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 all"))
    await prepare(coding)
    dispatch = await coding.dispatch()
    assert dispatch["args"]["input"].startswith("/goal ")  # Existing stage behavior is preserved.
    await report(
        coding,
        dispatch,
        {"outcome": "NEEDS_DESIGN_APPROVAL", "question": "Use a different storage design?"},
    )
    assert coding.state["status"] == "waiting-design-revision"
    assert (await coding.evaluate(_request("/workflow answer yes"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 1 revise Change storage"))
    await explore(coding, ("simple",))
    await present(coding)
    assert (await coding.evaluate(_request("/workflow designs 1 all"))).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 2 all"))
    assert (
        (await coding.evaluate(_call("workflow_prepare")))
        .data["definition"]["run_id"]
        .endswith("-designs-2")
    )


@pytest.mark.asyncio
async def test_final_comparison_requires_actual_evidence_and_separate_selection(coding):
    await start(coding)
    await explore(coding)
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 select simple"))
    await prepare(coding)
    while coding.state.get("current") != "compare-results":
        await finish(coding)
    assert (await coding.evaluate(_request("/workflow select simple"))).action == PolicyAction.DENY
    dispatch = await coding.dispatch()
    assert not dispatch["args"]["input"].startswith("/goal")
    result = {
        "results": [
            {
                "id": "simple",
                "behavior": "Greeting works",
                "tradeoffs": "Small change",
                "limitations": "English only",
                "evidence": [
                    "simple/implement",
                    "simple/review-a",
                    "simple/review-b-standards",
                    "simple/review-b-spec",
                ],
            },
            {
                "id": "alternative",
                "behavior": "Not implemented",
                "tradeoffs": "Not measured",
                "limitations": "Not selected for implementation",
                "evidence": [],
            },
        ],
        "recommendation": {"id": "simple", "reasons": "Passed shared checks and all reviews."},
    }
    result["results"][0]["evidence"].append("simple/unrecorded-test")
    await report(coding, dispatch, result)
    assert coding.state["status"] == "blocked"
    assert (await coding.evaluate(_request("/workflow select simple"))).action == PolicyAction.DENY
    result["results"][0]["evidence"].pop()
    await coding.evaluate(_request("/workflow retry"))
    dispatch = await coding.dispatch()
    await report(coding, dispatch, result)
    assert coding.state["status"] == "waiting-final-selection"
    assert (await coding.evaluate(_request("/workflow select simple"))).action == PolicyAction.DENY
    presentation = await present(coding)
    assert "Not approved for implementation" in presentation
    assert "Recorded evidence:" in presentation and "Checks passed" in presentation
    assert "English only" in presentation
    await coding.evaluate(_request("proceed"))
    assert coding.state["status"] == "waiting-final-selection"
    assert (
        await coding.evaluate(_request("/workflow select alternative"))
    ).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow select simple"))
    await snapshot(coding)
    assert coding.state["status"] == "complete"
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_cancel_keeps_proposals_and_cannot_restart_implementation(coding):
    await start(coding)
    await explore(coding)
    await present(coding)
    proposals = coding.state["designs"]
    await coding.evaluate(_request("/workflow cancel"))
    await coding.evaluate(_request("/workflow designs 1 all"))
    await coding.evaluate(_request("/workflow retry"))
    assert coding.state["status"] == "cancelled"
    assert coding.state["designs"] == proposals
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_compare_button_intent_cannot_be_downgraded_to_implement(coding):
    await coding.evaluate(
        _request(
            "Prepare the start message for `compare-review-fix` using the task below."
            "\nImprove greetings"
        )
    )
    definition = {
        "workflow": "implement-review-fix",
        "repository": "/tmp/repository",
        "task": "Improve greetings",
        "baseline": "HEAD",
        "base": "main",
        "candidates": [{"id": "a", "task": "One design"}],
    }
    assert (
        await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    ).action == PolicyAction.DENY
    definition.pop("workflow")
    await coding.evaluate(_request("/workflow start " + json.dumps(definition)))
    assert coding.state["current"] == "explore"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing-risk", "duplicate-id", "unknown-recommendation"])
async def test_incomplete_proposals_cannot_open_the_approval_gate(coding, invalid):
    await start(coding)
    dispatch = await coding.dispatch()
    value = design_report()
    if invalid == "missing-risk":
        del value["designs"][0]["risks"]
    elif invalid == "duplicate-id":
        value["designs"][1]["id"] = value["designs"][0]["id"]
    else:
        value["recommendation"]["id"] = "missing"
    await report(coding, dispatch, value)
    assert coding.state["status"] == "blocked"
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_unapproved_preparation_result_is_rejected(coding):
    await start(coding)
    await explore(coding)
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 select simple"))
    definition = (await coding.evaluate(_call("workflow_prepare"))).data["definition"]
    await coding.evaluate(
        output(
            "workflow_prepare",
            {
                "run_id": definition["run_id"],
                "candidates": [{"id": "alternative", "task": "Implement unapproved design"}],
            },
        )
    )
    assert coding.state["status"] == "blocked"
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_failed_candidates_are_compared_but_never_selectable(coding):
    await start(coding)
    await explore(coding)
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 all"))
    await prepare(coding)
    for identifier in ("simple", "alternative"):
        dispatch = await coding.dispatch()
        await report(
            coding,
            dispatch,
            {
                "outcome": "BLOCKED",
                "evidence": f"{identifier}: acceptance check failed",
                "question": "Required dependency is unavailable",
            },
        )
        await coding.evaluate(_request(f"/workflow reject {identifier} Dependency unavailable"))
        if coding.state["status"] == "snapshot":
            await snapshot(coding)
    assert coding.state["current"] == "compare-results"
    dispatch = await coding.dispatch()
    await report(
        coding,
        dispatch,
        {
            "results": [
                {
                    "id": identifier,
                    "behavior": "Could not implement",
                    "tradeoffs": "Not measured",
                    "limitations": "Dependency unavailable",
                    "evidence": [],
                }
                for identifier in ("simple", "alternative")
            ],
            "recommendation": {"id": None, "reasons": "No candidate passed the required checks."},
        },
    )
    assert coding.state["status"] == "waiting-final-selection"
    text = await present(coding)
    assert "Recommendation: none" in text
    assert "simple: acceptance check failed" in text
    assert "alternative: acceptance check failed" in text
    assert (await coding.evaluate(_request("/workflow select simple"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_clarification_during_exploration_resumes_exploration_only(coding):
    await start(coding)
    dispatch = await coding.dispatch()
    await report(coding, dispatch, {"outcome": "NEEDS_INPUT", "question": "What latency target?"})
    assert coding.state["status"] == "waiting-user"
    await coding.evaluate(_request("/workflow answer Under one second"))
    assert coding.state["current"] == "explore" and coding.state["status"] == "ready"
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_parallel_design_change_takes_priority_over_clarification(coding):
    await start(coding)
    await explore(coding, ("simple",))
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 all"))
    await prepare(coding)
    while coding.state.get("current") != "simple/review-b-standards":
        await finish(coding)
    standards = await coding.dispatch()
    spec = await coding.dispatch()
    await report(coding, standards, {"outcome": "NEEDS_INPUT", "question": "Which style?"})
    await report(
        coding, spec, {"outcome": "NEEDS_DESIGN_APPROVAL", "question": "Different storage needed"}
    )
    assert coding.state["status"] == "waiting-design-revision"
    assert "Which style?" in coding.state["question"]
    assert "Different storage needed" in coding.state["question"]
    assert (await coding.evaluate(_request("/workflow answer yes"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_first", [False, True])
@pytest.mark.parametrize("failure_kind", ["failed", "absent"])
async def test_parallel_failure_recovery_preserves_design_approval_request(
    coding, db_uri, monkeypatch, failure_first, failure_kind
):
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await start(coding)
    await explore(coding, ("simple",))
    await present(coding)
    await coding.evaluate(_request("/workflow designs 1 all"))
    await prepare(coding)
    while coding.state.get("current") != "simple/review-b-standards":
        await finish(coding)
    standards, spec = await coding.dispatch(), await coding.dispatch()
    if failure_kind == "failed":
        failed = coding.store.create_conversation(
            title="worker:" + spec["title"],
            parent_conversation_id=coding.session_id,
            labels={"omnigent.subagent.dispatch_id": "failed-work"},
        )
        coding.store.set_session_live_status(failed.id, "failed")
        failure = _completion(spec["title"], child_id=failed.id, status="failed")
    else:
        failure = output(
            "sys_session_send", "Error: dispatch response lost", {"args": {"title": spec["title"]}}
        )
    if failure_first:
        await coding.evaluate(failure)
    await report(
        coding,
        standards,
        {
            "outcome": "NEEDS_DESIGN_APPROVAL",
            "question": "Different storage needed",
        },
    )
    if not failure_first:
        await coding.evaluate(failure)
    coding.store = SqlAlchemyConversationStore(db_uri)
    await coding.evaluate(output("workflow_reconcile", {}))
    if failure_kind == "absent":
        assert coding.state["status"] == "blocked"
        await coding.evaluate(_request("/workflow retry-dispatch"))
    assert coding.state["status"] == "waiting-design-revision"
    assert "Different storage needed" in coding.state["question"]
    assert (
        coding.state["incomplete"]["simple/review-b-standards"]["outcome"]
        == "NEEDS_DESIGN_APPROVAL"
    )
    assert (await coding.evaluate(_request("/workflow retry"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_request("/workflow answer yes"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    await coding.evaluate(_request("/workflow designs 1 revise Change storage"))
    assert coding.state["current"] == "explore"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["evidence", "checks", "reasons"])
async def test_malformed_analysis_report_survives_restart_and_format_retry(coding, db_uri, field):
    await start(coding)
    dispatch = await coding.dispatch()
    original = design_report(("simple",))
    if field == "reasons":
        original["recommendation"][field] = ["Preserved research"]
    else:
        original[field] = ["Preserved research"]
    await report(coding, dispatch, original)
    assert coding.state["status"] == "blocked"
    assert field in coding.state["question"]
    assert "string" in coding.state["question"]
    saved = coding.state["incomplete"]["explore"]
    assert saved["designs"] == original["designs"]
    before = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == before
    await coding.evaluate(_request("/workflow answer Correct only the report format"))
    retry = await coding.dispatch()
    brief = analysis_brief(coding.state)
    assert "Preserved research" in brief
    assert "evidence (nonempty string)" in brief
    assert "checks (nonempty string)" in brief
    assert "reasons (nonempty string)" in brief
    await report(coding, retry, design_report(("simple",)))
    assert coding.state["status"] == "waiting-design-selection"
    assert coding.state["designs"]["approved"] == []
    assert coding.state["definition"]["publish"] is False
    assert (await coding.evaluate(_call("workflow_prepare"))).action == PolicyAction.DENY
    await present(coding)
    await coding.evaluate(_request("continue"))
    assert coding.state["status"] == "waiting-design-selection"
