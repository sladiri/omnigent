"""Conditional review scheduling through persisted policy events and content snapshots."""

import copy
import json

import pytest

from omnigent.spec.types import PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.workflows.controller import STATE_KEY, _generated_graph_path
from tests.policies.test_workflow_controller import (
    _call,
    _completion,
    _request,
    begin,
    finish,
    snapshot,
)
from tests.policies.test_workflow_controller import (
    coding as coding,
)


async def clean_reviews(coding):
    await begin(coding, workflow="review-fix", publish=False)
    await finish(coding)
    await finish(coding)
    assert coding.state["verified"] == {"one": "tree-1"}
    assert set(coding.state["completed"]) == {
        "one/review-a",
        "one/review-b-standards",
        "one/review-b-spec",
    }


@pytest.mark.parametrize("late_publish", [False, True])
async def test_graph_only_selection_retains_all_evidence_and_publication_gates(
    coding, late_publish
):
    await clean_reviews(coding)
    evidence = copy.deepcopy(coding.state["completed"])
    await coding.evaluate(_request("/workflow select one"))
    if late_publish:
        await snapshot(coding)
        await coding.evaluate(_request("/workflow publish one"))
    files = {"source.py": "tree-1", "graphify-out/graph.json": "new-graph"}
    await snapshot(coding, "graph-revision", files=files)
    assert coding.state["completed"] == evidence
    assert coding.state["verified"] == {"one": "graph-revision"}
    assert coding.state["selected"] == "one"
    assert coding.state["status"] == ("publish" if late_publish else "complete")
    assert coding.state["scheduling"][-1]["action"] == "retain-evidence"
    assert coding.state["snapshots"]["one"]["files"] == files
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    if late_publish:
        assert coding.state["publication_authorized_revision"] == "graph-revision"


@pytest.mark.parametrize(
    "path",
    [
        "source.py",
        "tests/test_source.py",
        "pyproject.toml",
        "AGENTS.md",
        "graphify-out/code.py",
        "graphify-out/wiki/AGENTS.md",
        "graphify-out/wiki/CLAUDE.local.md",
    ],
)
async def test_mixed_graph_and_relevant_changes_require_reviews_and_fresh_selection(coding, path):
    await clean_reviews(coding)
    await coding.evaluate(_request("/workflow select one"))
    files = {"source.py": "tree-1", "graphify-out/graph.json": "new", path: "changed"}
    await snapshot(coding, "mixed-revision", files=files)
    assert coding.state["selected"] is None
    assert coding.state["verified"] == {}
    assert coding.state["reviews"]["one"] == {}
    assert coding.state["current"] == "one/review-a"
    assert (await coding.evaluate(_call("workflow_publish"))).action == PolicyAction.DENY
    assert (await coding.evaluate(_request("/workflow select one"))).action == PolicyAction.DENY


async def test_missing_path_evidence_cannot_claim_graph_only_change(coding):
    await clean_reviews(coding)
    state = coding.state
    state["snapshots"]["one"].pop("files")
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    await coding.evaluate(_request("/workflow select one"))
    await snapshot(coding, "changed", files={"graphify-out/graph.json": "new"})
    assert coding.state["verified"] == {}
    assert coding.state["selected"] is None


@pytest.mark.parametrize("stage", ["fix-a", "fix-b", "verify-a", "verify-b-spec"])
async def test_controller_refuses_work_without_findings_or_an_actual_fix(coding, stage):
    await begin(coding)
    state = coding.state
    state["current"] = "one/" + stage
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state["pending"] is None
    assert "dispatch_serial" not in coding.state


@pytest.mark.parametrize(
    "stage", ["review-c", "fix-c", "verify-c", "final-b-standards", "final-b-spec"]
)
async def test_retired_stages_cannot_be_dispatched_even_from_saved_state(coding, stage):
    await begin(coding)
    state = coding.state
    state["current"] = "one/" + stage
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    result = await coding.evaluate(_call())
    assert result.action == PolicyAction.DENY
    assert "retired" in result.reason
    assert coding.state["pending"] is None


async def test_previous_scheduler_state_is_preserved_without_dispatch(coding):
    await begin(coding)
    state = coding.state
    state.pop("scheduler_version")
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY
    assert coding.state == state


async def test_actual_fix_and_independent_recheck_survive_restart(coding, db_uri):
    await begin(coding, workflow="review-fix", publish=False)
    await finish(coding, findings=[{"id": "bug", "message": "Input is not validated"}])
    review = coding.state["completed"]["one/review-a"]
    before = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == before
    await finish(coding, revision="fixed")
    assert coding.state["current"] == "one/verify-a"
    assert coding.state["findings"] == {"bug": "Input is not validated"}
    before = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == before
    await finish(coding)
    assert coding.state["current"] == "one/review-b-standards"
    assert coding.state["findings"] == {}
    assert coding.state["completed"]["one/review-a"] == review
    assert (
        coding.state["completed"]["one/fix-a"]["dispatch"]["child_id"]
        != (coding.state["completed"]["one/verify-a"]["dispatch"]["child_id"])
    )
    await finish(coding)
    assert coding.state["verified"] == {"one": "fixed"}
    assert len(coding.state["evidence_history"]) == 5


async def test_clean_recheck_cannot_clear_findings_without_resolution_evidence(coding):
    await begin(coding, workflow="review-fix")
    await finish(coding, findings=[{"id": "bug", "message": "Invalid input"}])
    await finish(coding, revision="fixed")
    dispatch = await coding.dispatch()
    await coding.evaluate(_completion(dispatch["title"]))
    await snapshot(coding)
    assert coding.state["findings"] == {"bug": "Invalid input"}
    assert coding.state["current"] == "one/fix-a"
    assert coding.state["verified"] == {}


async def test_graph_only_fix_starts_no_verifier_and_keeps_unresolved_findings(coding):
    await begin(coding, workflow="review-fix")
    await finish(coding, findings=[{"id": "bug", "message": "Invalid input"}])
    dispatch = await coding.dispatch()
    ctx = _completion(dispatch["title"])
    report = json.loads(ctx.content["result"])
    report["resolved"] = ["bug"]
    ctx.content["result"] = json.dumps(report)
    await coding.evaluate(ctx)
    await snapshot(
        coding, "graph", files={"source.py": "tree-1", "graphify-out/graph.json": "new"}
    )
    assert coding.state["status"] == "blocked"
    assert coding.state["findings"] == {"bug": "Invalid input"}
    assert not coding.state.get("rechecks")
    assert (await coding.evaluate(_call())).action == PolicyAction.DENY


async def test_only_the_axis_with_findings_gets_a_targeted_recheck(coding):
    await begin(coding, workflow="review-fix")
    await finish(coding)
    standards, spec = await coding.dispatch(), await coding.dispatch()
    await coding.evaluate(_completion(standards["title"], child_id="standards"))
    ctx = _completion(spec["title"], child_id="spec")
    report = json.loads(ctx.content["result"])
    report["findings"] = [{"id": "bug", "message": "Wrong output"}]
    ctx.content["result"] = json.dumps(report)
    await coding.evaluate(ctx)
    await snapshot(coding)
    await snapshot(coding)
    prior = coding.state["completed"]["one/review-b-standards"]
    await finish(coding, revision="fixed")
    assert coding.state["rechecks"] == ["verify-b-spec"]
    assert coding.state["current"] == "one/verify-b-spec"
    await finish(coding)
    assert coding.state["completed"]["one/review-b-standards"] == prior
    assert coding.state["verified"] == {"one": "fixed"}
    assert "one/verify-b-standards" not in coding.state["completed"]


@pytest.mark.parametrize("valid", [True, False])
async def test_broader_review_requires_concrete_reason_and_actual_paths(coding, valid):
    await begin(coding, workflow="review-fix")
    await finish(coding)
    await finish(coding, findings=[{"id": "bug", "message": "Shared behavior changed"}])
    await finish(coding, revision="fixed")
    request = {
        "role": "review-a",
        "reason": "Validation changes the shared empty-input behavior",
        "paths": ["source.py" if valid else "untouched.py"],
    }
    await finish(coding, extra={"reopen_reviews": [request]})
    assert coding.state["verified"] == {}
    if valid:
        assert coding.state["current"] == "one/review-a"
        records = [row for row in coding.state["scheduling"] if row["action"] == "reopen-review"]
        assert records[0]["reason"] == request["reason"]
        assert records[0]["paths"] == ["source.py"]
        await finish(coding)
        assert coding.state["verified"] == {"one": "fixed"}
    else:
        assert coding.state["status"] == "blocked"
        assert "changed paths" in coding.state["question"]


def test_generated_graph_classifier_keeps_source_and_instructions_relevant():
    for path in (
        "graphify-out/graph.json",
        "graphify-out/GRAPH_REPORT.md",
        "graphify-out/wiki/index.md",
        "graphify-out/obsidian/node.md",
    ):
        assert _generated_graph_path(path)
    for path in (
        "src/graphify-out/graph.json",
        "graphify-out/wiki/AGENTS.md",
        "graphify-out/wiki/CLAUDE.local.md",
        "graphify-out/tests.py",
        "AGENTS.md",
    ):
        assert not _generated_graph_path(path)


async def test_recheck_question_preserves_answer_retry_and_completion_gates(coding):
    await begin(coding, workflow="review-fix")
    await finish(coding, findings=[{"id": "bug", "message": "Invalid input"}])
    await finish(coding, revision="fixed")
    await finish(coding, question="Does the conflicting requirement permit this behavior?")
    assert coding.state["status"] == "waiting-user"
    assert coding.state["rechecks"] == ["verify-a"]
    assert coding.state["verified"] == {}
    await coding.evaluate(_request("/workflow answer Keep the approved behavior"))
    await snapshot(coding)
    await finish(coding)
    assert coding.state["current"] == "one/review-b-standards"


@pytest.mark.parametrize("mixed", [False, True])
async def test_initial_graph_only_scope_schedules_no_reviews_but_mixed_scope_does(coding, mixed):
    await begin(coding, workflow="review-fix", publish=False)
    state = coding.state
    state.update(status="snapshot", snapshot_for="before")
    coding.store.set_session_state(coding.session_id, {STATE_KEY: state})
    paths = ["graphify-out/graph.json"] + (["source.py"] if mixed else [])
    from tests.policies.test_workflow_controller import output

    await coding.evaluate(
        output(
            "workflow_snapshot",
            {
                "revision": "tree-1",
                "branch": "candidate",
                "files": {"source.py": "tree-1"},
                "baseline_paths": paths,
            },
        )
    )
    assert coding.state["completed"] == {}
    if mixed:
        assert coding.state["current"] == "one/review-a"
        assert len(coding.state["applicable_reviews"]["one"]) == 3
        assert coding.state["status"] == "ready"
    else:
        assert coding.state["applicable_reviews"]["one"] == []
        assert coding.state["verified"] == {"one": "tree-1"}
        assert coding.state["status"] == "waiting-user"
        assert (await coding.evaluate(_call())).action == PolicyAction.DENY


def test_archived_bundle_routing_stays_readable_without_restoring_retired_stages():
    from omnigent.workflows.controller import make_controller

    assert callable(make_controller(stages={"fix-c": {"model": "gpt-6.1-sol"}}))


@pytest.mark.parametrize("mixed", [False, True])
async def test_graph_housekeeping_fix_needs_no_recheck_but_mixed_fix_does(coding, mixed):
    await begin(coding, workflow="review-fix", publish=False)
    await finish(coding)
    await finish(
        coding,
        findings=[
            {
                "id": "graph",
                "message": "Refresh required graph output",
                "paths": ["graphify-out/graph.json"],
            }
        ],
    )
    evidence = coding.state["completed"].copy()
    dispatch = await coding.dispatch()
    ctx = _completion(dispatch["title"])
    report = json.loads(ctx.content["result"])
    report["resolved"] = list(coding.state["findings"])
    ctx.content["result"] = json.dumps(report)
    await coding.evaluate(ctx)
    files = {"source.py": "changed" if mixed else "tree-1", "graphify-out/graph.json": "new"}
    await snapshot(coding, "updated", files=files)
    for key, saved in evidence.items():
        assert coding.state["completed"][key] == saved
    if mixed:
        assert coding.state["current"] == "one/verify-b-standards"
        assert coding.state["findings"]
        assert coding.state["verified"] == {}
    else:
        assert coding.state["status"] == "waiting-user"
        assert coding.state["verified"] == {"one": "updated"}
        assert coding.state["findings"] == {}
        assert not any("/verify-" in key for key in coding.state["completed"])
        assert (await coding.evaluate(_call())).action == PolicyAction.DENY


@pytest.mark.parametrize("recheck", [False, True])
async def test_failed_parallel_dispatch_retains_completed_sibling_across_restart(
    coding, db_uri, monkeypatch, recheck
):
    from tests.policies.test_workflow_controller import output

    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: coding.store)
    await begin(coding, workflow="review-fix")
    await finish(coding)
    if recheck:
        await finish(coding, findings=[{"id": "bug", "message": "Invalid input"}])
        await finish(coding, revision="fixed")
    standards, spec = await coding.dispatch(), await coding.dispatch()
    ctx = _completion(standards["title"], child_id="standards")
    report = json.loads(ctx.content["result"])
    report["resolved"] = [key for key in coding.state["findings"] if key.startswith("standards/")]
    ctx.content["result"] = json.dumps(report)
    await coding.evaluate(ctx)
    failed_child = coding.store.create_conversation(
        title="worker:" + spec["title"], parent_conversation_id=coding.session_id
    )
    await coding.evaluate(_completion(spec["title"], child_id=failed_child.id, status="failed"))
    coding.store.set_session_live_status(failed_child.id, "failed")
    await coding.evaluate(output("workflow_reconcile", {}))
    assert coding.state["pending"] is None
    saved = coding.state
    coding.store = SqlAlchemyConversationStore(db_uri)
    assert coding.state == saved
    await coding.evaluate(_request("/workflow retry"))
    await snapshot(coding)
    next_dispatch = await coding.dispatch()
    assert next_dispatch["title"].split("/")[1] == (
        "verify-b-spec" if recheck else "review-b-spec"
    )
    assert "parallel" not in coding.state
    role = "one/" + ("verify-b-standards" if recheck else "review-b-standards")
    assert coding.state["completed"][role]["dispatch"]["title"] == standards["title"]


@pytest.mark.parametrize("fix", [False, True])
async def test_implementation_and_fix_cannot_complete_with_new_unresolved_findings(coding, fix):
    await begin(coding, publish=False)
    if fix:
        await finish(coding)
        await finish(coding, findings=[{"id": "original", "message": "Missing validation"}])
    await finish(coding, findings=[{"id": "new", "message": "Another actionable defect"}])
    assert coding.state["status"] == "blocked"
    assert "actionable findings" in coding.state["question"]
    assert coding.state["verified"] == {}
    assert (await coding.evaluate(_request("/workflow select one"))).action == PolicyAction.DENY
