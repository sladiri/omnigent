"""A small policy-backed workflow probe, with state owned by Omnigent."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text

STATE_KEY = "workflow_controller"
_READ_TOOLS = {
    "workflow_status",
    "sys_read_inbox",
    "sys_session_list",
    "sys_session_get_info",
    "sys_session_get_history",
}


def make_controller(
    candidates: list[dict[str, str]],
    stages: list[dict[str, str]],
    max_attempts: int = 2,
):
    """Build a controller for a fixed, ordered candidate/stage matrix.

    Definitions are trusted agent configuration. Workers return evidence;
    only request events can answer questions or select a candidate.
    """
    for entries, required in ((candidates, "task"), (stages, "prompt")):
        ids = [entry.get("id", "") for entry in entries]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Candidates and stages must have unique IDs")
        if any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", item) for item in ids):
            raise ValueError("Use lowercase kebab-case IDs")
        if any(not entry.get(required, "").strip() for entry in entries):
            raise ValueError(f"Each entry needs {required}")
    if isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    definition = hashlib.sha256(
        json.dumps([candidates, stages, max_attempts], sort_keys=True).encode()
    ).hexdigest()
    plan = [(candidate, stage) for candidate in candidates for stage in stages]
    keys = [f"{candidate['id']}/{stage['id']}" for candidate, stage in plan]

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") not in {"request", "tool_call", "tool_result"}:
            return {"result": "ALLOW"}
        if not event.get("context", {}).get("conversation_id"):
            return {"result": "DENY", "reason": "Workflow requires a session identity"}
        saved = event.get("session_state", {}).get(STATE_KEY)
        state = (
            copy.deepcopy(saved)
            if isinstance(saved, dict)
            else {
                "definition": definition,
                "status": "ready",
                "current": keys[0],
                "attempt": 1,
                "pending": None,
                "completed": {},
                "selected": None,
                "question": None,
            }
        )
        if state["definition"] != definition:
            return {"result": "DENY", "reason": "Workflow definition changed; use a new session"}

        def result(verdict="ALLOW", reason=None, **extra):
            return {
                "result": verdict,
                "reason": reason,
                "state_updates": [{"key": STATE_KEY, "action": "set", "value": state}],
                **extra,
            }

        phase, target = event.get("type"), event.get("target")
        if phase == "request":
            text = request_user_text(event.get("data")).strip()
            selection = re.fullmatch(r"/workflow select ([a-z0-9-]+)", text)
            if selection:
                if state["status"] != "waiting-user" or state["current"] is not None:
                    return result("DENY", "Selection requires all candidate stages to pass")
                if selection[1] not in {candidate["id"] for candidate in candidates}:
                    return result("DENY", "Unknown candidate")
                state.update(status="complete", selected=selection[1], question=None)
            elif text == "/workflow retry" or text.startswith("/workflow answer "):
                answering = text.startswith("/workflow answer ")
                expected = "waiting-user" if answering else "blocked"
                if state["status"] != expected or state["current"] is None:
                    return result("DENY", "No matching stage is waiting for this command")
                if state["pending"] is not None:
                    return result(
                        "DENY", "Dispatch is uncertain; reconcile the existing child first"
                    )
                if state["attempt"] >= max_attempts:
                    return result("DENY", "Attempt limit reached; inspect the recorded evidence")
                answer = text.removeprefix("/workflow answer ").strip() if answering else ""
                if answering and not answer:
                    return result("DENY", "An answer is required")
                state.update(status="ready", attempt=state["attempt"] + 1, question=None)
                state["answer"] = answer
            return result()

        if phase == "tool_call":
            if target in _READ_TOOLS:
                return result()
            if target != "sys_session_send":
                return result(
                    "DENY", "The coordinator may only dispatch stages and inspect sessions"
                )
            if state["status"] != "ready":
                return result("DENY", "Workflow is not ready; read its status and drain the inbox")
            candidate, stage = plan[keys.index(state["current"])]
            title = f"{state['current']}/{state['attempt']}"
            brief = "\n".join(
                [
                    f"Stage: {state['current']}",
                    f"Task: {candidate['task']}",
                    f"Workspace: {candidate.get('workspace', 'No source edits in this probe')}",
                    stage["prompt"],
                    "Follow repository instructions. Verify the assigned worktree before edits.",
                    "Do not commit, push, open a PR, deploy, or advance another stage.",
                    "Return only JSON with stage, outcome (COMPLETE, NEEDS_INPUT, BLOCKED),",
                    "and nonempty evidence. Include question for NEEDS_INPUT or BLOCKED.",
                    "COMPLETE attests that this stage's checks passed. Never infer a user answer.",
                    f"Previous evidence: {json.dumps(state['completed'], sort_keys=True)}",
                    f"User answer: {state.get('answer', '')}",
                ]
            )
            state.update(
                status="running",
                pending={
                    "stage": state["current"],
                    "title": title,
                    "agent": "worker",
                },
            )
            return result(data={"agent": "worker", "title": title, "args": brief})

        if phase != "tool_result":
            return result()
        if target == "workflow_status":
            return result(data=json.dumps(state, sort_keys=True))
        if target != "sys_session_send":
            return result()
        request = event.get("request_data") or {}
        pending = state["pending"]
        metadata = request.get("subagent")
        data = event.get("data") or {}
        output = data.get("result", "")
        if metadata is None:
            # The immediate handle is not evidence of successful stage completion.
            if pending:
                call_args = request.get("arguments", request.get("args", {}))
                if call_args.get("title") and call_args["title"] != pending["title"]:
                    return result()
                try:
                    handle = json.loads(output)
                except (ValueError, TypeError):
                    handle = {}
                if isinstance(handle, dict) and handle.get("title") == pending["title"]:
                    pending["child_id"] = handle.get("conversation_id")
                elif not isinstance(handle, dict) or not handle.get("title"):
                    state.update(
                        status="blocked", question="Dispatch uncertain; inspect child sessions"
                    )
            return result()

        args = request.get("args", {})
        work_id = metadata.get("work_id")
        if any(item["work_id"] == work_id for item in state["completed"].values()):
            return result()
        if not pending or args.get("title") != pending["title"]:
            return result()
        if args.get("agent") != pending["agent"] or not work_id or not args.get("conversation_id"):
            return result("DENY", "Completion is missing its dispatch identity")
        if pending.get("child_id") and args["conversation_id"] != pending["child_id"]:
            return result("DENY", "Completion belongs to another child")
        state["pending"] = None
        state["last_result"] = {**metadata, "child_id": args["conversation_id"], "output": output}
        if metadata.get("status") != "completed":
            state.update(status="blocked", question=f"Child ended with {metadata.get('status')}")
            return result()
        try:
            report = json.loads(output)
        except (ValueError, TypeError):
            report = None
        if (
            not isinstance(report, dict)
            or report.get("stage") != state["current"]
            or not isinstance(report.get("evidence"), str)
            or not report["evidence"].strip()
        ):
            state.update(status="blocked", question="Child returned an invalid stage report")
            return result()
        if report.get("outcome") != "COMPLETE":
            question = report.get("question")
            if (
                report.get("outcome") not in {"NEEDS_INPUT", "BLOCKED"}
                or not isinstance(question, str)
                or not question.strip()
            ):
                state.update(
                    status="blocked", question="Child returned an invalid outcome or question"
                )
            else:
                state.update(
                    status="waiting-user" if report["outcome"] == "NEEDS_INPUT" else "blocked",
                    question=question,
                )
            return result()
        state["completed"][state["current"]] = {
            "evidence": report["evidence"],
            "work_id": work_id,
            "child_id": args["conversation_id"],
        }
        index = keys.index(state["current"]) + 1
        state.update(
            current=keys[index] if index < len(keys) else None,
            status="ready" if index < len(keys) else "waiting-user",
            attempt=1,
            question=None
            if index < len(keys)
            else "Select a candidate with /workflow select <id>",
        )
        state.pop("answer", None)
        return result()

    return evaluate
