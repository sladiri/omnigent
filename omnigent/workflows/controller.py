"""Persisted coding workflow gates, implemented as an optional function policy."""

from __future__ import annotations

import copy
import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias, cast

from omnigent.policies.builtins.github import github_policy
from omnigent.policies.builtins.orchestration import blast_radius, read_only_os
from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
STATE_KEY = "workflow_controller"
STAGES = (
    "implement",
    "review-a",
    "fix-a",
    "verify-a",
    "review-b-standards",
    "review-b-spec",
    "fix-b",
    "verify-b-standards",
    "verify-b-spec",
    "review-c",
    "fix-c",
    "verify-c",
    "final-b-standards",
    "final-b-spec",
)
READ_TOOLS = {
    "workflow_status",
    "sys_read_inbox",
    "sys_session_list",
    "sys_session_get_info",
    "sys_session_get_history",
}
_REVIEW = {stage for stage in STAGES if stage.startswith(("review-", "verify-", "final-"))}


def _deny(reason: str) -> PolicyResponse:
    return {"result": "DENY", "reason": reason}


def _result(state: Json, **extra: Any) -> PolicyResponse:  # type: ignore[explicit-any]
    return cast(
        PolicyResponse,
        {
            "result": "ALLOW",
            "state_updates": [{"key": STATE_KEY, "action": "set", "value": state}],
            **extra,
        },
    )


def _decode(event: PolicyEvent) -> Json:
    data = event.get("data")
    raw = data.get("result") if isinstance(data, dict) else data
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _stage(state: Json) -> str:
    return str(state.get("current", "")).split("/")[-1]


def _candidate(state: Json) -> Json:
    candidate_id = state.get("selected") or str(state["current"]).split("/")[0]
    return next(c for c in state["candidates"] if c["id"] == candidate_id)


def _set_stage(state: Json, candidate: Json, stage: str) -> None:
    state.pop("parallel", None)
    state.update(
        current=f"{candidate['id']}/{stage}",
        status="snapshot",
        snapshot_for="before",
        attempt=1,
        pending=None,
    )


def _pending(state: Json) -> list[Json]:
    return [
        pending
        for pending in (state.get("pending"), state.get("parallel", {}).get("pending"))
        if pending
    ]


def _advance_candidate(state: Json) -> None:
    remaining = next(
        (
            candidate
            for candidate in state["candidates"]
            if candidate["id"] not in state["verified"]
            and candidate["id"] not in state.get("rejected", {})
        ),
        None,
    )
    state.pop("revision", None)
    state["findings"] = {}
    if remaining:
        _set_stage(state, remaining, "implement")
    else:
        state.update(
            status="waiting-user" if state["verified"] else "blocked",
            current=None,
            question="Compare the candidate evidence, then send /workflow select <id>"
            if state["verified"]
            else "No viable candidate remains; start a new workflow",
        )


def _block(state: Json, question: str) -> PolicyResponse:
    state.update(status="blocked", question=question)
    return _result(state)


def _definition(raw: str, conversation_id: str) -> Json:
    definition = json.loads(raw)
    if not isinstance(definition, dict):
        raise ValueError("Definition must be an object")
    for key in ("repository", "task", "baseline", "base"):
        if not isinstance(definition.get(key), str) or not definition[key].strip():
            raise ValueError(f"Definition needs {key}")
    if not Path(definition["repository"]).is_absolute():
        raise ValueError("Repository must be an absolute path")
    candidates = definition.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Definition needs candidates")
    ids = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate.get("id", "")
        ):
            raise ValueError("Candidate IDs must use lowercase kebab-case")
        if not isinstance(candidate.get("task"), str) or not candidate["task"].strip():
            raise ValueError("Each candidate needs a task")
        ids.append(candidate["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Candidate IDs must be unique")
    if type(definition.get("publish", False)) is not bool:
        raise ValueError("publish must be boolean")
    definition["run_id"] = conversation_id
    definition.setdefault("remote", "origin")
    return definition


def _worker_policy(event: PolicyEvent, root_id: str, session_id: str) -> PolicyResponse:
    """Grant tools only to the recorded worker for the outstanding stage."""
    from omnigent.runtime import get_conversation_store

    store = get_conversation_store()
    root, child = store.get_conversation(root_id), store.get_conversation(session_id)
    state = root.session_state.get(STATE_KEY, {}) if root else {}
    if not child or child.parent_conversation_id != root_id:
        return _deny("Only the active workflow worker may use tools")
    pending = next(
        (
            item
            for item in _pending(state)
            if child.id == item.get("child_id") or child.title == "worker:" + item["title"]
        ),
        None,
    )
    if not pending or pending.get("stopped"):
        return _deny("This worker is no longer the active workflow stage")
    if state.get("status") != "running":
        return _deny("The workflow is paused")
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    target = str(event.get("target", ""))
    if target.startswith("workflow_") or target in {
        "sys_session_send",
        "sys_session_create",
        "sys_add_policy",
    }:
        return _deny("Workers cannot advance or reconfigure the workflow")
    data = event.get("data")
    args = data.get("arguments", {}) if isinstance(data, dict) else {}
    workspace = Path(_candidate(state)["workspace"]).resolve()
    skill_roots = [Path.home() / parent / "skills" for parent in (".agents", ".codex", ".claude")]
    skill_paths = [
        installed.resolve()
        for root in skill_roots
        if root.is_dir()
        for installed in root.iterdir()
    ]
    for key in ("path", "file_path", "notebook_path", "cwd", "workdir"):
        path = args.get(key)
        if isinstance(path, str):
            if not Path(path).is_absolute():
                return _deny("Worker file and directory paths must be absolute")
            resolved = Path(path).resolve()
            skill_read = target in {"sys_os_read", "Read"} and any(
                resolved.is_relative_to(root) for root in skill_paths
            )
            if not resolved.is_relative_to(workspace) and not skill_read:
                return _deny("Worker paths must stay in the candidate or installed skill files")
    if target in {"sys_os_write", "sys_os_edit", "Write", "Edit", "apply_patch"}:
        paths = [args.get(key) for key in ("path", "file_path", "notebook_path")]
        if target == "apply_patch" or not any(
            isinstance(path, str) and Path(path).is_absolute() for path in paths
        ):
            return _deny("Use sys_os_write or sys_os_edit with an absolute candidate path")
    if "command" in args or "cmd" in args:
        cwd = args.get("workdir", args.get("cwd"))
        command = str(args.get("command", args.get("cmd", "")))
        explicit_cd = command.startswith(f"cd {shlex.quote(str(workspace))} && ")
        if cwd != str(workspace) and not explicit_cd:
            return _deny("Shell calls must explicitly set the candidate workdir")
    for policy in (github_policy(),):
        verdict = policy(event)
        if verdict and verdict.get("result") != "ALLOW":
            return verdict
    verdict = blast_radius(risky_action="DENY")(cast(Json, event), {})
    if verdict["result"] != "ALLOW":
        return cast(PolicyResponse, verdict)
    command = str(args.get("command", args.get("cmd", "")))
    if re.search(
        r"\bgit\b[^\n;]*\b(commit|merge|rebase|cherry-pick|push|reset)\b|\b(oc|kubectl)\b", command
    ):
        return _deny("Workers cannot publish or rewrite Git history")
    if pending["stage"].split("/")[-1] in _REVIEW:
        verdict = read_only_os()(cast(Json, event), {})
        if verdict["result"] != "ALLOW":
            return cast(PolicyResponse, verdict)
    return {"result": "ALLOW"}


def _brief(state: Json) -> str:
    candidate, stage = _candidate(state), _stage(state)
    review = stage in _REVIEW
    skill = "code-review" if "-b-" in stage else "ponytail and ponytail-review"
    task = (
        f"Load installed {skill} skills. Review the full task diff and connected code, "
        "including staged, unstaged and untracked files. Do not edit."
        if review
        else "Implement the candidate and run its acceptance checks."
        if stage == "implement"
        else "Fix all recorded findings and run affected checks. "
        "Report disputed findings as BLOCKED."
    )
    axes = (
        f"Perform only the {stage.rsplit('-', 1)[1]} axis of the code-review skill. "
        "The other axis runs in a separate fresh session. Do not merge its findings."
        if "-b-" in stage
        else ""
    )
    return "\n".join(
        [
            f"Stage: {state['current']}",
            f"Task: {state['definition']['task']}",
            f"Candidate: {candidate['task']}",
            f"Worktree: {candidate['workspace']}",
            f"Branch: {candidate['branch']}",
            f"Review baseline: {candidate['baseline']}",
            f"Reviewed revision: {state['revision']}",
            task,
            axes,
            "Verify the exact worktree and branch before any command. Start every shell "
            f"command with `cd {shlex.quote(candidate['workspace'])} &&`. "
            "Native hooks do not preserve workdir. Follow repository instructions. "
            "Use sys_os_write or sys_os_edit with absolute candidate paths for edits; "
            "native apply_patch is not supported by this controller.",
            "Do not commit, push, create a PR, deploy, or advance another workflow stage. "
            "Native delegation for independent read-only review is allowed; Omnigent session "
            "dispatch is reserved to the controller.",
            "Return only JSON: stage, outcome (COMPLETE/NEEDS_INPUT/BLOCKED), evidence "
            "(nonempty string), findings (list of {id,message}), resolved (list of finding "
            "IDs). NEEDS_INPUT/BLOCKED also needs question.",
            "COMPLETE attests required checks passed. Rechecks must inspect actual revised "
            "code. Missing skills or unavailable required delegation are BLOCKED, never "
            "silently skip them.",
            f"Outstanding findings: {json.dumps(state.get('findings', {}))}",
            f"Prior evidence: {json.dumps(state['completed'])}",
            f"User answer: {state.get('answer', '')}",
        ]
    )


def _findings(report: Json) -> dict[str, str]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Findings must be a list")
    result = {}
    for item in findings:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k].strip() for k in ("id", "message")
        ):
            raise ValueError("Each finding needs id and message")
        if item["id"] in result:
            raise ValueError("Duplicate finding ID")
        result[item["id"]] = item["message"]
    return result


def _accept_snapshot(state: Json, snapshot: Json, max_attempts: int) -> PolicyResponse:
    if not isinstance(snapshot.get("revision"), str) or not snapshot["revision"]:
        return _block(state, "Snapshot failed; retry the snapshot before continuing")
    candidate, stage = _candidate(state), _stage(state)
    if snapshot.get("branch") != candidate["branch"]:
        return _block(state, "Candidate branch changed")
    purpose = state["snapshot_for"]
    if purpose == "selection":
        if snapshot["revision"] != state["verified"].get(candidate["id"]):
            state["selected"] = None
            state.pop("publication_input", None)
            state["verified"].pop(candidate["id"], None)
            _set_stage(state, candidate, "review-a")
            state["question"] = "Selected candidate changed; reviews and selection must run again"
            return _result(state)
        state.update(
            status="publish" if state["definition"].get("publish") else "complete", question=None
        )
        return _result(state)
    if purpose == "before":
        if (
            state.get("revision")
            and snapshot["revision"] != state["revision"]
            and stage != "implement"
        ):
            _set_stage(state, candidate, "review-a")
            state["findings"] = {}
            state["verified"].pop(candidate["id"], None)
        state.update(status="ready", revision=snapshot["revision"], question=None)
        return _result(state)
    report = state.pop("report")
    if stage in _REVIEW and snapshot["revision"] != state["revision"]:
        state["verified"].pop(candidate["id"], None)
        return _block(
            state,
            "Read-only review changed the candidate; rerun reviews after restoring ownership",
        )
    state["revision"] = snapshot["revision"]
    parallel = state.pop("parallel", None)
    findings = _findings(report) if stage in _REVIEW else {}
    if parallel:
        findings = {}
        for key, axis_report in parallel["reports"].items():
            state["completed"][key] = {**axis_report, "revision": state["revision"]}
            axis = key.rsplit("-", 1)[1]
            findings.update({axis + "/" + k: v for k, v in _findings(axis_report).items()})
        stage = stage.removesuffix("standards") + "spec"
        state["current"] = candidate["id"] + "/" + stage
    else:
        state["completed"][state["current"]] = {**report, "revision": state["revision"]}
    if stage.startswith("fix-") and not set(state["findings"]).issubset(
        set(report.get("resolved", []))
    ):
        return _block(state, "Fix did not account for every outstanding finding")
    if stage in _REVIEW:
        state["findings"] = findings
    if findings and stage.startswith(("verify-", "final-")) and not stage.endswith("standards"):
        fix = "fix-b" if "-b-" in stage else "fix-" + stage[-1]
        count = state["fix_attempts"].get(candidate["id"] + "/" + fix, 0)
        if count >= max_attempts:
            return _block(state, "Review fix attempt limit reached")
        _set_stage(state, candidate, fix)
        return _result(state)
    if stage == "final-b-spec":
        state["verified"][candidate["id"]] = state["revision"]
        _advance_candidate(state)
    else:
        next_stage = STAGES[STAGES.index(stage) + 1]
        _set_stage(state, candidate, next_stage)
    return _result(state)


def _reconcile(state: Json, root_id: str) -> PolicyResponse:
    from omnigent.runtime import get_conversation_store

    outstanding = _pending(state)
    if not outstanding:
        return _result(state, data=json.dumps(state))
    store = get_conversation_store()
    ids = store.list_child_conversation_ids_by_parent([root_id]).get(root_id, [])
    children = store.get_conversations(ids)
    for pending in outstanding:
        pending.pop("reconciled_absent", None)
        pending.pop("reconciled_unstarted", None)
    for pending in outstanding:
        pending.pop("stopped", None)
        matching = [
            child for child in children.values() if child.title == "worker:" + pending["title"]
        ]
        if len(matching) > 1:
            return _block(
                state, "Multiple matching children; stop them before resolving ownership"
            )
        if matching:
            child = matching[0]
            pending["child_id"] = child.id
            if child.live_status == "failed" or (
                child.live_status == "idle"
                and pending.get("terminal_status") in {"failed", "cancelled"}
            ):
                pending["stopped"] = True
            elif (
                child.live_status in {None, "idle"}
                and not child.labels.get("omnigent.subagent.dispatch_id")
                and not store.list_items(child.id, limit=1).data
            ):
                pending["reconciled_unstarted"] = True
        else:
            pending["reconciled_absent"] = True
    if all(item.get("stopped") for item in outstanding):
        state["pending"] = None
        state.pop("parallel", None)
        return _block(state, "Persisted child failure confirmed; inspect evidence then retry")
    if any(
        item.get("reconciled_absent") or item.get("reconciled_unstarted") for item in outstanding
    ):
        return _block(
            state,
            "Dispatch has no persisted start evidence. Confirm all old dispatches stopped, "
            "then send /workflow retry-dispatch",
        )
    state.update(
        status="running",
        question="Existing children recovered; drain the native inbox without resending. "
        "An idle child without a completion needs operator investigation; it is not proof "
        "that the dispatch stopped.",
    )
    return _result(state, data=json.dumps(state))


def _coding_controller(max_attempts: int) -> Callable[[PolicyEvent], PolicyResponse]:
    def evaluate(event: PolicyEvent) -> PolicyResponse:
        phase, target = event.get("type"), event.get("target")
        if phase not in {"request", "tool_call", "tool_result"}:
            return {"result": "ALLOW"}
        context = event.get("context", {})
        session_id = context.get("conversation_id")
        if not session_id:
            return _deny("Workflow requires a trusted session identity")
        root_id = context.get("root_conversation_id") or session_id
        if root_id != session_id:
            return _worker_policy(event, root_id, session_id)
        state = cast(
            Json,
            copy.deepcopy(event.get("session_state", {}).get(STATE_KEY) or {"status": "idle"}),
        )
        if phase == "request":
            text = request_user_text(event.get("data")).strip()
            if text.startswith("/workflow start "):
                if state["status"] != "idle":
                    return _deny("This session already owns a workflow; use a new session")
                try:
                    definition = _definition(text.removeprefix("/workflow start "), session_id)
                except (ValueError, TypeError) as exc:
                    return _deny(str(exc))
                state = {
                    "status": "prepare",
                    "definition": definition,
                    "completed": {},
                    "verified": {},
                    "findings": {},
                    "fix_attempts": {},
                    "selected": None,
                    "pending": None,
                    "question": None,
                }
            elif text.startswith("/workflow select "):
                candidate_id = text.removeprefix("/workflow select ")
                if (
                    state["status"] != "waiting-user"
                    or state.get("current") is not None
                    or candidate_id not in state.get("verified", {})
                ):
                    return _deny("Selection requires all candidate stages to pass")
                state.update(selected=candidate_id, status="snapshot", snapshot_for="selection")
            elif text.startswith("/workflow answer "):
                answer = text.removeprefix("/workflow answer ").strip()
                if state["status"] != "waiting-user" or not state.get("current") or not answer:
                    return _deny("No stage is waiting for this answer")
                state.update(
                    answer=answer,
                    status="snapshot",
                    snapshot_for="before",
                    attempt=state["attempt"] + 1,
                    question=None,
                )
            elif text == "/workflow retry-dispatch":
                if (
                    state["status"] != "blocked"
                    or not _pending(state)
                    or not all(
                        item.get("reconciled_absent") or item.get("reconciled_unstarted")
                        for item in _pending(state)
                    )
                ):
                    return _deny(
                        "Reconcile the uncertain dispatch before authorizing a new attempt"
                    )
                _reconcile(state, session_id)
                if not all(
                    item.get("reconciled_absent") or item.get("reconciled_unstarted")
                    for item in _pending(state)
                ):
                    return _result(state)
                state.pop("parallel", None)
                state.update(
                    status="snapshot",
                    snapshot_for="before",
                    pending=None,
                    attempt=state["attempt"] + 1,
                )
            elif text.startswith("/workflow reject "):
                parts = text.removeprefix("/workflow reject ").split(" ", 1)
                if (
                    len(parts) != 2
                    or not parts[1].strip()
                    or state["status"] != "blocked"
                    or _pending(state)
                    or not state.get("current")
                    or parts[0] != _candidate(state)["id"]
                ):
                    return _deny(
                        "Reject only the stopped current candidate, with an explicit reason"
                    )
                candidate = _candidate(state)
                state.setdefault("rejected", {})[candidate["id"]] = parts[1].strip()
                state["verified"].pop(candidate["id"], None)
                _advance_candidate(state)
            elif text == "/workflow retry":
                if state["status"] != "blocked" or _pending(state):
                    return _deny("Resolve or reconcile the pending stage first")
                if (
                    state.get("candidates")
                    and not state.get("current")
                    and not state.get("selected")
                ):
                    return _deny("No candidate remains; start a new workflow session")
                if state.get("selected"):
                    state.update(status="snapshot", snapshot_for="selection")
                elif not state.get("candidates"):
                    state["status"] = "prepare"
                else:
                    state.update(
                        status="snapshot",
                        snapshot_for="before",
                        attempt=state.get("attempt", 1) + 1,
                    )
            return _result(state)
        if phase == "tool_call":
            if target in READ_TOOLS or target == "workflow_reconcile":
                return _result(state)
            if target == "workflow_prepare" and state["status"] == "prepare":
                return _result(state, data={"definition": state["definition"]})
            if target == "workflow_snapshot" and state["status"] == "snapshot":
                return _result(state, data={"workspace": _candidate(state)["workspace"]})
            if (
                target == "workflow_publish"
                and state["status"] == "publish"
                and state.get("selected")
            ):
                definition, candidate = state["definition"], _candidate(state)
                if "publication_input" not in state:
                    data = event.get("data")
                    supplied = data.get("arguments", {}) if isinstance(data, dict) else {}
                    metadata = {
                        key: supplied.get(key, definition.get(key)) for key in ("title", "body")
                    }
                    if not all(
                        isinstance(value, str) and value.strip() for value in metadata.values()
                    ):
                        return _deny(
                            "Publication requires title and body describing the reviewed result"
                        )
                    from omnigent.workflows.operations import validate_publication_text

                    try:
                        validate_publication_text(
                            cast(str, metadata["title"]),
                            cast(str, metadata["body"]),
                            state.get("pr_template", ""),
                        )
                    except ValueError as exc:
                        return _deny(str(exc))
                    state["publication_input"] = metadata
                return _result(
                    state,
                    data={
                        "workspace": candidate["workspace"],
                        "revision": state["verified"][candidate["id"]],
                        **state["publication_input"],
                        "base": definition["base"],
                        "remote": definition["remote"],
                    },
                )
            paired_stage = state.get("parallel", {}).get("next")
            if target != "sys_session_send" or (
                state["status"] != "ready" and not (state["status"] == "running" and paired_stage)
            ):
                return _deny("Action is not allowed in the current workflow state")
            if state["attempt"] > max_attempts + 1:
                return {**_block(state, "Stage attempt limit reached"), "result": "DENY"}
            stage = _stage(state)
            if stage.startswith("fix-"):
                count = state["fix_attempts"].get(state["current"], 0)
                if count >= max_attempts:
                    return {**_block(state, "Fix attempt limit reached"), "result": "DENY"}
                state["fix_attempts"][state["current"]] = count + 1
            # A nonce prevents a revisited stage from continuing an old child turn.
            serial = state.get("dispatch_serial", 0) + 1
            current = paired_stage or state["current"]
            title = f"{current}/{serial}"
            pending = {"title": title, "stage": current, "agent": "worker"}
            brief_state = {**state, "current": current}
            state.update(
                status="running",
                dispatch_serial=serial,
            )
            if paired_stage:
                state["parallel"].update(next=None, pending=pending)
            else:
                state["pending"] = pending
                if stage.endswith("-b-standards"):
                    state["parallel"] = {
                        "next": current.removesuffix("standards") + "spec",
                        "pending": None,
                        "reports": {},
                    }
            return _result(
                state,
                data={
                    "agent": "worker",
                    "title": title,
                    "args": _brief(brief_state),
                    "workspace": _candidate(state)["workspace"],
                },
            )
        if target == "workflow_status":
            return _result(state, data=json.dumps(state, sort_keys=True))
        if target == "workflow_reconcile":
            return _reconcile(state, session_id)
        result = _decode(event)
        if target == "workflow_prepare" and state["status"] == "prepare":
            if not result.get("candidates") or result.get("run_id") != session_id:
                return _block(
                    state, "Worktree preparation failed; inspect tool error before retry"
                )
            state["candidates"] = result["candidates"]
            state["pr_template"] = result.get("pr_template", "")
            _set_stage(state, state["candidates"][0], "implement")
            return _result(state)
        if target == "workflow_snapshot" and state["status"] == "snapshot":
            try:
                return _accept_snapshot(state, result, max_attempts)
            except (ValueError, TypeError, KeyError) as exc:
                return _block(state, f"Invalid stage evidence: {exc}")
        if target == "workflow_publish" and state["status"] == "publish":
            if not result.get("commit") or not result.get("pr_url"):
                return _block(
                    state, "Publication incomplete; retry reconciles commit, remote and PR"
                )
            state.update(status="complete", publication=result, question=None)
            return _result(state)
        if target != "sys_session_send":
            return _result(state)
        request = cast(Json, event.get("request_data") or {})
        metadata = request.get("subagent")
        call_args = cast(Json, request.get("arguments", request.get("args", {})))
        title = call_args.get("title", result.get("title"))
        pending = next((item for item in _pending(state) if item["title"] == title), None)
        if not metadata:
            if pending and result.get("title") == pending["title"]:
                pending["child_id"] = result.get("conversation_id")
            elif (pending or (not title and _pending(state))) and not result.get("title"):
                return _block(state, "Dispatch uncertain; call workflow_reconcile")
            return _result(state)
        args = request.get("args", {})
        if not pending or args.get("title") != pending["title"]:
            return _result(state)
        if (
            not metadata.get("work_id")
            or not args.get("conversation_id")
            or args.get("agent") != "worker"
        ):
            return _deny("Child completion has no trusted dispatch identity")
        if pending.get("child_id") and args["conversation_id"] != pending["child_id"]:
            return _deny("Child completion belongs to another dispatch")
        if metadata.get("status") != "completed":
            pending["child_id"] = args["conversation_id"]
            pending["terminal_status"] = metadata.get("status")
            return _block(
                state,
                "Worker failed or was cancelled; call workflow_reconcile to confirm it stopped",
            )
        if pending is state.get("pending"):
            state["pending"] = None
        else:
            state["parallel"]["pending"] = None
        state["last_result"] = {**metadata, "child_id": args["conversation_id"]}
        result["dispatch"] = {
            **state["last_result"],
            "title": pending["title"],
        }
        if (
            result.get("stage") != pending["stage"]
            or not isinstance(result.get("evidence"), str)
            or not result["evidence"].strip()
        ):
            result = {
                "stage": pending["stage"],
                "outcome": "BLOCKED",
                "question": "Worker returned invalid stage evidence",
                "evidence": "Invalid worker response",
            }
        if result.get("outcome") == "COMPLETE":
            try:
                _findings(result)
                resolved = result.get("resolved")
                if not isinstance(resolved, list) or not all(
                    isinstance(item, str) and item.strip() for item in resolved
                ):
                    raise ValueError("Resolved findings must be a list of IDs")
            except ValueError as exc:
                result.update(outcome="BLOCKED", question=f"Invalid stage evidence: {exc}")
        parallel = state.get("parallel")
        if parallel:
            parallel["reports"][pending["stage"]] = result
            if _pending(state) or parallel["next"]:
                return _result(state)
            result = next(
                (
                    report
                    for report in parallel["reports"].values()
                    if report.get("outcome") != "COMPLETE"
                ),
                parallel["reports"][state["current"]],
            )
        if result.get("outcome") != "COMPLETE":
            state.pop("parallel", None)
            if (
                result.get("outcome") not in {"BLOCKED", "NEEDS_INPUT"}
                or not isinstance(result.get("question"), str)
                or not result["question"].strip()
            ):
                return _block(state, "Worker returned invalid outcome")
            state.update(
                status="waiting-user" if result["outcome"] == "NEEDS_INPUT" else "blocked",
                question=result["question"],
            )
            return _result(state)
        state.update(status="snapshot", snapshot_for="after", report=result)
        return _result(state)

    return evaluate


def make_controller(max_attempts: int = 2) -> Callable[[PolicyEvent], PolicyResponse]:
    """Build the persisted coding workflow controller."""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    return _coding_controller(max_attempts)
