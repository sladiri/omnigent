"""Persisted coding workflow gates, implemented as an optional function policy."""

from __future__ import annotations

import copy
import json
import re
import shlex
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias, cast

from omnigent.entities import Conversation
from omnigent.models.model_override import validate_model_override
from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    UnsupportedShellSyntax,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.policies.builtins.github import github_policy
from omnigent.policies.builtins.orchestration import blast_radius, read_only_os
from omnigent.policies.builtins.working_dir import _GIT_VALUE_OPTS, block_working_dir_changes
from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text
from omnigent.stores import ConversationStore
from omnigent.util.reasoning_effort import CODEX_NATIVE_EFFORTS
from omnigent.workflows import comparison

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
STATE_KEY = "workflow_controller"
WORKFLOWS = (comparison.NAME, "implement-review-fix", "review-fix")
POLICY_REGISTRY = [
    {
        "handler": "omnigent.workflows.controller.make_controller",
        "kind": "factory",
        "name": "Coding Workflow Controller",
        "description": "Enforce implementation, review, selection, and publication gates.",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_attempts": {"type": "integer", "minimum": 1, "default": 2},
                "worker_defaults": {"type": "object"},
                "stages": {"type": "object"},
            },
            "additionalProperties": False,
        },
    }
]
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
LINEAR_READ_OPERATIONS = {
    "get_agent_context",
    "get_agent_skill",
    "get_attachment",
    "get_diff",
    "get_diff_threads",
    "get_document",
    "get_initiative",
    "get_issue",
    "get_issue_status",
    "get_milestone",
    "get_notifications",
    "get_project",
    "get_release",
    "get_release_note",
    "get_status_updates",
    "get_team",
    "get_template",
    "get_triage_responsibility",
    "get_user",
    "get_workspace",
    "list_agent_skills",
    "list_comments",
    "list_custom_views",
    "list_customer_needs",
    "list_customers",
    "list_cycles",
    "list_diffs",
    "list_documents",
    "list_initiative_labels",
    "list_initiatives",
    "list_issue_labels",
    "list_issue_statuses",
    "list_issues",
    "list_milestones",
    "list_project_labels",
    "list_projects",
    "list_release_notes",
    "list_release_pipelines",
    "list_releases",
    "list_teams",
    "list_templates",
    "list_users",
    "search",
    "search_documentation",
}
LINEAR_TICKET_OPERATIONS = {"create_issue", "update_issue", "save_issue"}


def _linear_permission(target: str, *, may_edit: bool) -> PolicyResponse | None:
    # Native hooks preserve the separator flattened by JavaScript aliases.
    for prefix in ("mcp__codex_apps__linear__", "mcp__codex_apps__linear_"):
        if target.startswith(prefix):
            operation = target.removeprefix(prefix)
            if operation in LINEAR_READ_OPERATIONS or (
                may_edit and operation in LINEAR_TICKET_OPERATIONS
            ):
                return {"result": "ALLOW"}
            return _deny("Linear action is not allowed for this workflow role")
    return None


def _github_permission(event: PolicyEvent) -> PolicyResponse | None:
    prefixes = (
        "mcp__codex_apps__github__",
        "mcp__codex_apps__github_",
        "mcp__github__",
        "github__",
    )
    target = str(event.get("target", ""))
    for prefix in prefixes:
        if target.startswith(prefix):
            if target.removeprefix(prefix) in {"fetch", "search", "compare_commits"}:
                return {"result": "ALLOW"}
            return github_policy(mcp_tool_prefixes=list(prefixes))(event) or {"result": "ALLOW"}
    return None


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
    if state.get("current") in comparison.ANALYSIS_STAGES:
        return {"workspace": state["definition"]["repository"]}
    candidate_id = state.get("selected") or str(state["current"]).split("/")[0]
    return next(c for c in state["candidates"] if c["id"] == candidate_id)


def _set_stage(state: Json, candidate: Json, stage: str) -> None:
    state.pop("parallel", None)
    state.pop("retry_authorization", None)
    state.update(
        current=f"{candidate['id']}/{stage}",
        status="snapshot",
        snapshot_for="before",
        attempt=1,
        pending=None,
    )


def _retry_stage(state: Json) -> None:
    state.update(
        status="ready" if state.get("current") in comparison.ANALYSIS_STAGES else "snapshot",
        snapshot_for="before",
        attempt=state.get("attempt", 1) + 1,
        question=None,
    )
    state["retry_authorization"] = {"stage": state["current"], "attempt": state["attempt"]}


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
    state.pop("review_wait", None)
    state["findings"] = {}
    if remaining:
        first_stage = (
            "review-a" if state["definition"]["workflow"] == "review-fix" else "implement"
        )
        _set_stage(state, remaining, first_stage)
    elif comparison.enabled(state):
        comparison.analysis_stage(state, "compare-results")
    else:
        state.update(
            status="waiting-user" if state["verified"] else "blocked",
            current=None,
            question="Review the candidate evidence, then send /workflow select <id>"
            if state["verified"]
            else "No viable candidate remains; start a new workflow",
        )


def _block(state: Json, question: str) -> PolicyResponse:
    state.update(status="blocked", question=question)
    return _result(state)


def _execution_settings(
    definition: Json, worker_defaults: Json | None = None, stages: Json | None = None
) -> Json:
    """Validate and freeze bundle defaults plus per-run stage overrides."""

    def settings(value: object) -> Json:
        if not isinstance(value, dict) or set(value) - {"model", "reasoning_effort"}:
            raise ValueError("Worker settings allow only model and reasoning_effort")
        result = dict(value)
        if "model" in result:
            if not isinstance(result["model"], str):
                raise ValueError("Worker model must be a string")
            result["model"] = validate_model_override(result["model"])
        if "reasoning_effort" in result and (
            not isinstance(result["reasoning_effort"], str)
            or result["reasoning_effort"] not in CODEX_NATIVE_EFFORTS
        ):
            raise ValueError("Unsupported worker reasoning_effort")
        return result

    defaults = {
        **settings(worker_defaults if worker_defaults is not None else {}),
        **settings(definition.get("worker_defaults", {})),
    }
    overrides = {}
    for mapping in (stages if stages is not None else {}, definition.get("stages", {})):
        if (
            not isinstance(mapping, dict)
            or set(mapping) - set(STAGES) - comparison.ANALYSIS_STAGES
        ):
            raise ValueError("Worker stages must name existing workflow stages")
        for stage, value in mapping.items():
            overrides[stage] = {**overrides.get(stage, {}), **settings(value)}
    return {"worker_defaults": defaults, "stages": overrides}


def _definition(
    raw: str, conversation_id: str, worker_defaults: Json | None = None, stages: Json | None = None
) -> Json:
    definition = json.loads(raw)
    if not isinstance(definition, dict):
        raise ValueError("Definition must be an object")
    for key in ("repository", "task", "baseline", "base"):
        if not isinstance(definition.get(key), str) or not definition[key].strip():
            raise ValueError(f"Definition needs {key}")
    if not Path(definition["repository"]).is_absolute():
        raise ValueError("Repository must be an absolute path")
    candidates = definition.get("candidates", [])
    workflow = definition.setdefault(
        "workflow",
        comparison.NAME
        if isinstance(candidates, list) and len(candidates) > 1
        else "implement-review-fix",
    )
    if workflow not in WORKFLOWS:
        raise ValueError("Unknown workflow")
    if not isinstance(candidates, list) or (not candidates and workflow != comparison.NAME):
        raise ValueError("Definition needs candidates")
    if workflow == "review-fix" and len(candidates) != 1:
        raise ValueError("Review needs exactly one current implementation")
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
    definition.update(_execution_settings(definition, worker_defaults, stages))
    definition["run_id"] = conversation_id
    definition.setdefault("remote", "origin")
    return definition


def draft_start_message(
    workflow: str,
    repository: str,
    task: str,
    baseline: str,
    base: str,
    publish: bool = False,
    branch: str = "",
    worker_defaults: dict[str, str] | None = None,
    stages: dict[str, dict[str, str]] | None = None,
) -> str:
    """Validate a proposed start command without starting work or changing state."""
    try:
        if branch and workflow == comparison.NAME:
            raise ValueError("Compare requires separate branches for approved designs")
        definition = _definition(
            json.dumps(
                {
                    "workflow": workflow,
                    "repository": repository,
                    "task": task,
                    "baseline": baseline,
                    "base": base,
                    "publish": publish,
                    "worker_defaults": worker_defaults if worker_defaults is not None else {},
                    "stages": stages if stages is not None else {},
                    "candidates": []
                    if workflow == comparison.NAME
                    else [
                        {
                            "id": "current" if workflow == "review-fix" else "implementation",
                            "task": task,
                            **({"branch": branch} if branch else {}),
                        }
                    ],
                }
            ),
            "",
        )
    except (ValueError, TypeError) as exc:
        return json.dumps({"valid": False, "error": str(exc)})
    definition.pop("run_id")
    return json.dumps({"valid": True, "command": "/workflow start " + json.dumps(definition)})


def question_preview(state: Json) -> Json | None:
    """Bind the UI answer to this exact persisted stage question."""
    if (
        state.get("status") not in {"waiting-user", "blocked"}
        or not state.get("question")
        or not state.get("current")
        or state.get("selected")
        or _pending(state)
    ):
        return None
    identity = {
        key: state.get(key)
        for key in ("definition", "status", "current", "attempt", "revision", "question")
    }
    return {
        "token": uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(identity, sort_keys=True)).hex,
        "text": state["question"],
    }


def draft_preview(session_state: Json) -> Json:
    """Return only the current draft details needed for user approval."""
    state = session_state.get(STATE_KEY, {})
    draft = state.get("draft") if state.get("status") == "idle" else None
    if not draft:
        return {"draft": None, "question": question_preview(state)}
    definition = draft["definition"]
    return {
        "draft": {
            "token": draft["token"],
            **{
                key: definition[key]
                for key in ("workflow", "repository", "task", "baseline", "base")
            },
            "branch": next((c.get("branch") for c in definition["candidates"]), None),
            "publish": definition.get("publish", False),
        }
    }


def _repository_read_permission(event: PolicyEvent, repository: str | None) -> PolicyResponse:
    if event.get("type") == "tool_result":
        return {"result": "ALLOW"}
    data = event.get("data")
    args = data.get("arguments", {}) if isinstance(data, dict) else {}
    if not isinstance(args, dict) or not repository or not Path(repository).is_absolute():
        return _deny("Select a source repository before reading it")
    return {"result": "ALLOW", "data": {**args, "repository": repository}}


def _journal_permission(event: PolicyEvent, repository: str | None) -> PolicyResponse:
    """Bind journal operations to the assigned repository without changing workflow state."""
    from omnigent.workflows.journal import ACTIONS

    if event.get("type") == "tool_result":
        return {"result": "ALLOW"}
    data = event.get("data")
    args = data.get("arguments", {}) if isinstance(data, dict) else {}
    if not isinstance(args, dict) or args.get("action") not in ACTIONS:
        return _deny("Use a supported workflow_journal action")
    if not repository or not Path(repository).is_absolute():
        return _deny("Select a repository before journaling")
    return {"result": "ALLOW", "data": {**args, "repository": repository}}


def _worker_git_mutation(command: str, depth: int = 0) -> bool:
    """Classify Git's subcommand, not words in paths or search expressions."""
    if depth > MAX_SHELL_NESTING:
        return True
    pattern = r"\bgit\b[^\n;]*\b(commit|merge(?!-base\b)|rebase|cherry-pick|push|reset)\b"
    try:
        segments = split_command_segments(command)
    except UnsupportedShellSyntax:
        # Unsupported shell syntax must not bypass the worker's Git guard.
        return True
    if len(segments) > 1:
        return any(_worker_git_mutation(segment, depth + 1) for segment in segments)
    try:
        tokens = real_invocation_tokens(shlex.split(command))
    except ValueError:
        return bool(re.search(pattern, command))
    if not tokens:
        return False
    inner = unwrap_shell_command(tokens)
    if inner is not None:
        return _worker_git_mutation(inner, depth + 1)
    if Path(tokens[0]).name != "git":
        # Keep the conservative guard for compound syntax and opaque programs.
        return bool(re.search(pattern, command))
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in _GIT_VALUE_OPTS:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            # Config options can execute Git through an alias or pager.
            return token in {"commit", "merge", "rebase", "cherry-pick", "push", "reset"} or bool(
                re.search(pattern, " ".join(tokens[1:i]))
            )
    return False


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
    target = str(event.get("target", ""))
    if target == "workflow_stage":
        if event.get("type") == "tool_result":
            return {"result": "ALLOW", "data": _brief({**state, "current": pending["stage"]})}
        return {"result": "ALLOW"}
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    linear = _linear_permission(
        target,
        may_edit=pending["stage"].split("/")[-1] in {"implement", "fix-a", "fix-b", "fix-c"},
    )
    if linear is not None:
        return linear
    github = _github_permission(event)
    if github is not None:
        return github
    if target == "workflow_read":
        return _repository_read_permission(event, state.get("definition", {}).get("repository"))
    if target == "workflow_journal":
        workspace = _candidate({**state, "current": pending["stage"]})["workspace"]
        return _journal_permission(event, workspace)
    if target.startswith("workflow_") or target in {
        "sys_session_send",
        "sys_session_create",
        "sys_add_policy",
    }:
        return _deny("Workers cannot advance or reconfigure the workflow")
    data = event.get("data")
    args = data.get("arguments", {}) if isinstance(data, dict) else {}
    stage = pending["stage"]
    workspace = Path(_candidate({**state, "current": stage})["workspace"]).resolve()
    if stage in comparison.ANALYSIS_STAGES:
        if "command" in args or "cmd" in args:
            if target not in {"sys_os_shell", "Bash", "exec_command", "shell_command"} or args.get(
                "command", args.get("cmd")
            ) not in _analysis_commands(workspace):
                return _deny(
                    "Exploration and comparison allow only the exact read commands in the brief"
                )
        elif target not in {"sys_os_read", "Read", "Glob", "Grep"}:
            return _deny("Exploration and comparison cannot implement, test, or delegate")
    skill_roots = [Path.home() / parent / "skills" for parent in (".agents", ".codex", ".claude")]
    skill_roots.append(Path.home() / ".codex/skills/.system")
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
            result_read = stage == "compare-results" and any(
                resolved.is_relative_to(Path(candidate["workspace"]).resolve())
                for candidate in state.get("candidates", [])
            )
            if not resolved.is_relative_to(workspace) and not skill_read and not result_read:
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
    command = str(args.get("command", args.get("cmd", "")))
    if _worker_git_mutation(command) or re.search(r"\b(oc|kubectl)\b", command):
        return _deny("Workers cannot publish or rewrite Git history")
    policy_event: PolicyEvent = event
    if "command" in args or "cmd" in args:
        # Resolve the brief's exact cd prefix before checking relative Git paths.
        prefix = f"cd {shlex.quote(str(workspace))} && "
        shell_args = {**args, "command": command.removeprefix(prefix)}
        if command.startswith(prefix):
            shell_args["workdir"] = str(workspace)
        policy_event = {
            **event,
            "target": "sys_os_shell",
            "context": {"conversation_id": session_id},
            "data": {"name": "sys_os_shell", "arguments": shell_args},
        }
    for policy in (
        github_policy(),
        block_working_dir_changes(
            block_worktree=False, allowed_dirs=[str(workspace)], allow_sibling_worktrees=False
        ),
        block_working_dir_changes(
            block_cd=False,
            allow_sibling_worktrees=stage.split("/")[-1]
            in {"implement", "fix-a", "fix-b", "fix-c"},
        ),
    ):
        verdict = policy(policy_event)
        if verdict and verdict.get("result") != "ALLOW":
            return verdict
    verdict = blast_radius(risky_action="DENY")(cast(Json, policy_event), {})
    if verdict["result"] != "ALLOW":
        return cast(PolicyResponse, verdict)
    if pending["stage"].split("/")[-1] in _REVIEW:
        verdict = read_only_os()(cast(Json, policy_event), {})
        if verdict["result"] != "ALLOW":
            return cast(PolicyResponse, verdict)
    return {"result": "ALLOW"}


def _analysis_commands(workspace: Path) -> set[str]:
    return {
        f"cd {shlex.quote(str(workspace))} && /usr/bin/env {command}"
        for command in (
            "git worktree list --porcelain",
            "git status --short",
            "git ls-files",
            "rg --files",
        )
    }


def _brief(state: Json) -> str:
    if state.get("current") in comparison.ANALYSIS_STAGES:
        commands = sorted(_analysis_commands(Path(state["definition"]["repository"]).resolve()))
        return comparison.brief(state) + "\nAllowed read commands:\n" + "\n".join(commands)
    candidate, stage = _candidate(state), _stage(state)
    review = stage in _REVIEW
    skill = "code-review" if "-b-" in stage else "ponytail and ponytail-review"
    task = (
        f"Load installed {skill} skills. Review the full task diff and connected code, "
        "including staged, unstaged and untracked files. Do not edit. "
        "Return COMPLETE with actionable in-scope defects in findings; the controller "
        "routes them to the fix stage automatically. Use NEEDS_INPUT only for a real "
        "user decision, missing information, conflicting instructions, or additional "
        "authorization; use BLOCKED for missing capabilities. The question must name "
        "the decision or blocker and explain why you cannot proceed. Never ask whether "
        "to fix routine findings or continue the authorized workflow."
        if review
        else "Implement the candidate and run its acceptance checks."
        if stage == "implement"
        else "Fix the recorded defects within the approved scope and run affected checks. "
        "Apply recorded user decisions. If a needed decision is missing or conflicts with "
        "required checks, return NEEDS_INPUT. Report disputed findings as BLOCKED."
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
            f'Copy the exact stage ID into the report: "stage": {json.dumps(state["current"])}. '
            "Do not shorten it to the operation name.",
            "For reviews and rechecks, COMPLETE means the inspection finished; remaining "
            "defects belong in findings and still require fixes and verification. For "
            "implementation and fixes, COMPLETE attests required checks passed. Rechecks "
            "must inspect actual revised code. Missing skills or unavailable required "
            "delegation are BLOCKED, never silently skip them. Omit question when no "
            "decision or blocker remains. Preserve explicitly accepted exceptions from "
            "the saved decisions; do not create or broaden exceptions.",
            "For Compare, implement only the approved design. A materially different design "
            "needs a new approval: stop and return outcome NEEDS_DESIGN_APPROVAL with question. "
            "Do not treat clarification as permission to change the design.",
            f"Outstanding findings: {json.dumps(state.get('findings', {}))}",
            f"Incomplete evidence: {json.dumps(state.get('incomplete', {}))}",
            f"Prior evidence: {json.dumps(state['completed'])}",
            f"User decisions: {json.dumps(state.get('decisions', []))}",
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


def _accept_snapshot(state: Json, snapshot: Json) -> PolicyResponse:
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
            state.pop("publication_authorized_revision", None)
            state["verified"].pop(candidate["id"], None)
            _set_stage(state, candidate, "review-a")
            state["question"] = "Selected candidate changed; reviews and selection must run again"
            return _result(state)
        state.update(
            status="publish"
            if (
                state["definition"].get("publish")
                or state.get("publication_authorized_revision") == snapshot["revision"]
            )
            else "complete",
            question=None,
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
    fix = ("fix-b" if "-b-" in stage else "fix-" + stage[-1]) if findings else None
    reports = parallel["reports"] if parallel else {state["current"]: report}
    if _wait_for_review(state, reports, next_stage=fix):
        return _result(state)
    if fix:
        _set_stage(state, candidate, fix)
        return _result(state)
    if stage == "final-b-spec":
        state["verified"][candidate["id"]] = state["revision"]
        _advance_candidate(state)
    else:
        next_stage = STAGES[STAGES.index(stage) + 1]
        _set_stage(state, candidate, next_stage)
    return _result(state)


def _wait_for_review(state: Json, reports: Json, *, next_stage: str | None = None) -> bool:
    issues = {
        key: report
        for key, report in reports.items()
        if key.split("/")[-1] in _REVIEW
        and (report.get("outcome") != "COMPLETE" or report.get("question"))
    }
    if not issues:
        return False
    details = []
    for key, report in issues.items():
        details.append(f"{key}: {report.get('evidence', '')}")
        if report.get("question"):
            details.append(str(report["question"]))
        try:
            findings = _findings(report)
        except ValueError:
            findings = {}
        details.extend(f"{identifier}: {message}" for identifier, message in findings.items())
    design_change = comparison.enabled(state) and any(
        report.get("outcome") == "NEEDS_DESIGN_APPROVAL" for report in issues.values()
    )
    command = (
        f"/workflow designs {state['designs']['revision']} revise <feedback>"
        if design_change
        else "/workflow answer <decision and instructions>"
    )
    state.update(
        status="waiting-design-revision" if design_change else "waiting-user",
        review_wait={"reports": reports, "next_stage": next_stage},
        question="\n".join(details) + f"\nReview stopped. Send {command}.",
    )
    return True


def _retire_dispatches(state: Json) -> bool:
    """Clear stopped dispatches without discarding required user decisions."""
    state["pending"] = None
    parallel = state.pop("parallel", {})
    reports = parallel.get("reports", {})
    state.setdefault("incomplete", {}).update(reports)
    if _wait_for_review(state, reports):
        return True
    return False


def _accept_worker_result(
    state: Json, pending: Json, result: Json, metadata: Json, child_id: str
) -> PolicyResponse:
    """Apply live and recovered reports through the same workflow gates."""
    if pending is state.get("pending"):
        state["pending"] = None
    else:
        state["parallel"]["pending"] = None
    state["last_result"] = {**metadata, "child_id": child_id}
    result["dispatch"] = {
        **state["last_result"],
        "title": pending["title"],
    }
    if (
        result.get("outcome") == "NEEDS_INPUT"
        and result.get("stage") == pending["stage"].rsplit("/", 1)[-1]
    ):
        result["stage"] = pending["stage"]
    if (
        result.get("stage") != pending["stage"]
        or not isinstance(result.get("evidence"), str)
        or not result["evidence"].strip()
    ):
        question = result.get("question")
        result.update(
            stage=pending["stage"],
            outcome="BLOCKED",
            question="Worker returned invalid stage evidence"
            + (f". Worker question: {question}" if isinstance(question, str) else ""),
        )
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
    if result.get("outcome") != "COMPLETE":
        state.setdefault("incomplete", {})[pending["stage"]] = result
    reports = {pending["stage"]: result}
    parallel = state.get("parallel")
    if parallel:
        parallel["reports"][pending["stage"]] = result
        if _pending(state) or parallel["next"]:
            return _result(state)
        reports = parallel["reports"]
        result = next(
            (
                report
                for report in reports.values()
                if report.get("outcome") == "NEEDS_DESIGN_APPROVAL"
            ),
            next(
                (report for report in reports.values() if report.get("outcome") != "COMPLETE"),
                parallel["reports"][state["current"]],
            ),
        )
    if result.get("outcome") != "COMPLETE" and _wait_for_review(state, reports):
        state.pop("parallel", None)
        state.setdefault("incomplete", {}).update(reports)
        return _result(state)
    if result.get("outcome") == "NEEDS_DESIGN_APPROVAL" and comparison.enabled(state):
        if _pending(state):
            return _block(
                state, "Stop and reconcile the remaining workers before revising designs"
            )
        state.update(
            status="waiting-design-revision",
            question=result.get("question")
            or "Revise the design proposals before implementing a different approach",
        )
        return _result(state)
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
    if state.get("current") in comparison.ANALYSIS_STAGES:
        try:
            comparison.accept(state, result)
        except (ValueError, TypeError, KeyError) as exc:
            return _block(state, f"Invalid comparison: {exc}")
        return _result(state)
    state.update(status="snapshot", snapshot_for="after", report=result)
    return _result(state)


def _recover_worker_question(
    store: ConversationStore, child: Conversation, pending: Json
) -> Json | None:
    """Recover a question, never success, from an idle native worker's last item."""
    if (
        pending.get("terminal_status") in {"failed", "cancelled"}
        or child.live_status != "idle"
        or child.labels.get("omnigent.wrapper") != "codex-native-ui"
        or not child.labels.get("omnigent.subagent.dispatch_id")
        or (pending.get("child_id") and pending["child_id"] != child.id)
    ):
        return None
    items = store.list_items(child.id, limit=1, order="desc").data
    if not items:
        return None
    item = items[0].to_api_dict()
    if (
        item.get("type") != "message"
        or item.get("role") != "assistant"
        or item.get("status") != "completed"
        or item.get("interrupted")
    ):
        return None
    try:
        report = json.loads(
            "\n".join(
                block.get("text", "")
                for block in item.get("content", [])
                if block.get("type") in {"text", "output_text"}
            )
        )
    except (ValueError, TypeError):
        return None
    if not isinstance(report, dict) or report.get("outcome") != "NEEDS_INPUT":
        return None
    if report.get("stage") not in {pending["stage"], pending["stage"].rsplit("/", 1)[-1]}:
        return None
    if not all(
        isinstance(report.get(key), str) and report[key].strip()
        for key in ("question", "evidence")
    ):
        return None
    return report


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
            if pending.get("child_id") and pending["child_id"] != child.id:
                return _block(state, "Recovered child does not match the saved dispatch")
            report = _recover_worker_question(store, child, pending)
            if report is not None:
                _accept_worker_result(
                    state,
                    pending,
                    report,
                    {
                        "status": "completed",
                        "work_id": child.labels["omnigent.subagent.dispatch_id"],
                        "recovered": True,
                    },
                    child.id,
                )
                continue
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
    outstanding = _pending(state)
    if not outstanding:
        return _result(state, data=json.dumps(state))
    if all(item.get("stopped") for item in outstanding):
        if _retire_dispatches(state):
            return _result(state)
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


def _coding_controller(
    max_attempts: int, execution: Json
) -> Callable[[PolicyEvent], PolicyResponse]:
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
        if phase in {"tool_call", "tool_result"}:
            linear = _linear_permission(str(target or ""), may_edit=True)
            if linear is not None:
                return linear if phase == "tool_call" else {"result": "ALLOW"}
            github = _github_permission(event)
            if github is not None:
                return github if phase == "tool_call" else {"result": "ALLOW"}
        if phase == "tool_result" and target == "workflow_review_checkout":
            return {"result": "ALLOW"}
        state = cast(
            Json,
            copy.deepcopy(event.get("session_state", {}).get(STATE_KEY) or {"status": "idle"}),
        )
        if target in {"workflow_journal", "workflow_read"} and phase in {
            "tool_call",
            "tool_result",
        }:
            from omnigent.runtime import get_conversation_store

            definition = (
                state.get("definition") or (state.get("draft") or {}).get("definition") or {}
            )
            repository = definition.get("repository")
            if not repository:
                session = get_conversation_store().get_conversation(session_id)
                repository = session.workspace if session else None
            if target == "workflow_read":
                return _repository_read_permission(event, repository)
            return _journal_permission(event, repository)
        if state["status"] == "cancelled":
            if phase == "tool_call" and target not in READ_TOOLS:
                return _deny("This workflow was cancelled")
            return _result(
                state,
                **(
                    {"data": json.dumps(state)}
                    if target == "workflow_status" and phase == "tool_result"
                    else {}
                ),
            )
        if phase == "request":
            # Only the server's user-message gate may apply decisions; native hooks echo it.
            if not isinstance(event.get("data"), dict) or context.get("harness") is not None:
                return {"result": "ALLOW"}
            approved_draft = False
            text = request_user_text(event.get("data")).strip()
            if text.startswith("/workflow answer-question "):
                token, _, answer = text.removeprefix("/workflow answer-question ").partition(" ")
                question = question_preview(state)
                if not question or question["token"] != token:
                    return _deny(
                        "This workflow question changed; review it again before answering"
                    )
                text = "/workflow answer " + answer
            if text.startswith("/workflow start-draft "):
                draft = state.get("draft")
                if (
                    state["status"] != "idle"
                    or not draft
                    or text != "/workflow start-draft " + draft["token"]
                ):
                    return _deny(
                        "This draft is no longer current. Prepare and review a new draft."
                    )
                text = draft["command"]
                approved_draft = True
            if state["status"] == "idle":
                if text:
                    state.pop("draft", None)
                for name in WORKFLOWS:
                    if text.startswith(
                        f"Prepare the start message for `{name}` using the task below."
                    ):
                        state["requested_workflow"] = name
            if comparison.enabled(state):
                try:
                    if comparison.command(state, text):
                        return _result(state)
                except (ValueError, TypeError) as exc:
                    return _deny(str(exc))
            if text.startswith("/workflow start "):
                if state["status"] != "idle":
                    return _deny("This session already owns a workflow; use a new session")
                try:
                    raw = json.loads(text.removeprefix("/workflow start "))
                    if not isinstance(raw, dict):
                        raise ValueError("Definition must be an object")
                    requested = state.get("requested_workflow")
                    if requested:
                        if raw.get("workflow", requested) != requested:
                            raise ValueError(
                                "Start message must use the workflow selected by the button"
                            )
                        raw["workflow"] = requested
                    definition = _definition(
                        json.dumps(raw), session_id, **({} if approved_draft else execution)
                    )
                except (ValueError, TypeError) as exc:
                    return _deny(str(exc))
                state = {
                    "status": "prepare",
                    "definition": definition,
                    "workflow_id": session_id,
                    "completed": {},
                    "verified": {},
                    "findings": {},
                    "fix_attempts": {},
                    "selected": None,
                    "pending": None,
                    "question": None,
                }
                if comparison.enabled(state):
                    comparison.analysis_stage(state, "explore")
            elif text.startswith("/workflow publish "):
                candidate_id = text.removeprefix("/workflow publish ")
                if (
                    state["status"] != "complete"
                    or state.get("publication")
                    or _pending(state)
                    or state.get("current") is not None
                    or candidate_id != state.get("selected")
                    or candidate_id not in state.get("verified", {})
                ):
                    return _deny("Publication requires a completed, selected, unpublished result")
                revision = state["verified"][candidate_id]
                state.setdefault("decisions", []).append({"command": text, "revision": revision})
                state.update(
                    publication_authorized_revision=revision,
                    status="snapshot",
                    snapshot_for="selection",
                )
            elif text.startswith("/workflow select "):
                candidate_id = text.removeprefix("/workflow select ")
                if (
                    state["status"]
                    != ("waiting-final-selection" if comparison.enabled(state) else "waiting-user")
                    or (comparison.enabled(state) and state.get("presented") != "results")
                    or state.get("current") is not None
                    or candidate_id not in state.get("verified", {})
                ):
                    return _deny("Selection requires all candidate stages to pass")
                state.setdefault("decisions", []).append(
                    {"command": text, "revision": state.get("designs", {}).get("revision")}
                )
                state.update(selected=candidate_id, status="snapshot", snapshot_for="selection")
            elif text.startswith("/workflow answer "):
                answer = text.removeprefix("/workflow answer ").strip()
                if not question_preview(state) or not answer:
                    return _deny("No stage is waiting for this answer")
                review_wait = state.pop("review_wait", {})
                state.setdefault("decisions", []).append(
                    {
                        "command": text,
                        "question": state["question"],
                        "stage": state["current"],
                        "revision": state.get("revision"),
                        "review": review_wait,
                    }
                )
                state["answer"] = answer
                _retry_stage(state)
                if review_wait.get("next_stage"):
                    _set_stage(state, _candidate(state), review_wait["next_stage"])
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
                if not _pending(state) or not all(
                    item.get("reconciled_absent") or item.get("reconciled_unstarted")
                    for item in _pending(state)
                ):
                    return _result(state)
                if _retire_dispatches(state):
                    return _result(state)
                _retry_stage(state)
            elif text.startswith("/workflow reject "):
                parts = text.removeprefix("/workflow reject ").split(" ", 1)
                if (
                    len(parts) != 2
                    or not parts[1].strip()
                    or not (
                        state["status"] == "blocked"
                        or (state["status"] == "waiting-user" and state.get("review_wait"))
                    )
                    or _pending(state)
                    or not state.get("current")
                    or state.get("current") in comparison.ANALYSIS_STAGES
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
                if state.get("current") in comparison.ANALYSIS_STAGES:
                    _retry_stage(state)
                elif state.get("selected"):
                    state.update(status="snapshot", snapshot_for="selection")
                elif not state.get("candidates"):
                    state["status"] = "prepare"
                else:
                    _retry_stage(state)
            return _result(state)
        if phase == "tool_call":
            if target == "workflow_review_checkout":
                if state["status"] != "idle" or state.get("requested_workflow") not in (
                    None,
                    "review-fix",
                ):
                    return _deny(
                        "PR checkout preparation is only available before starting Review"
                    )
                return {"result": "ALLOW"}
            if target == "workflow_draft" and state["status"] == "idle":
                state.pop("draft", None)
                data = event.get("data")
                args = data.get("arguments", {}) if isinstance(data, dict) else {}
                if not isinstance(args, dict):
                    return _deny("Draft arguments must be an object")
                if (
                    state.get("requested_workflow")
                    and args.get("workflow") != state["requested_workflow"]
                ):
                    return _deny("Draft must use the workflow selected by the button")
                try:
                    resolved = _execution_settings(args, **execution)
                except (ValueError, TypeError) as exc:
                    return _deny(str(exc))
                if not any(resolved.values()):
                    return _result(state)
                return _result(state, data={**args, **resolved})
            if target in READ_TOOLS or target == "workflow_reconcile":
                return _result(state)
            if target == "workflow_present" and state["status"] in {
                "waiting-design-selection",
                "waiting-final-selection",
            }:
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
            retry_authorized = state.get("retry_authorization") == {
                "stage": state["current"],
                "attempt": state["attempt"],
            }
            # The paired reviewer belongs to the already authorized dispatch.
            if not (retry_authorized or paired_stage) and state["attempt"] > max_attempts + 1:
                return {**_block(state, "Stage attempt limit reached"), "result": "DENY"}
            stage = _stage(state)
            if stage.startswith("fix-"):
                count = state["fix_attempts"].get(state["current"], 0)
                if count >= max_attempts and not retry_authorized:
                    return {**_block(state, "Fix attempt limit reached"), "result": "DENY"}
                state["fix_attempts"][state["current"]] = count + 1
            state.pop("retry_authorization", None)
            # A nonce prevents a revisited stage from continuing an old child turn.
            serial = state.get("dispatch_serial", 0) + 1
            current = paired_stage or state["current"]
            title = f"{current}/{serial}"
            pending = {"title": title, "stage": current, "agent": "worker"}
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
                    "args": {
                        **state["definition"].get("worker_defaults", {}),
                        **state["definition"]
                        .get("stages", {})
                        .get(current.rsplit("/", 1)[-1], {}),
                        "input": (
                            f"Stage: {current}\n"
                            "Read workflow_stage and return its report. Do not create a goal."
                            if current in comparison.ANALYSIS_STAGES
                            else f"/goal Stage: {current}\n"
                            "Produce the final report for your assigned workflow stage. "
                            "First call workflow_stage for the complete task, worktree, checks, "
                            "and report contract. Work only on that stage. Continue until its "
                            "checks are done or a user decision or external blocker prevents "
                            "progress. Then mark this report goal complete and return the "
                            "required JSON, using NEEDS_INPUT or BLOCKED when appropriate. "
                            "Completing this report goal does not attest that the stage passed. "
                            "The controller owns stage advancement and user decisions."
                        ),
                    },
                    "workspace": _candidate(state)["workspace"],
                },
            )
        if target == "workflow_draft" and state["status"] == "idle":
            state.pop("draft", None)
            result = _decode(event)
            if result.get("valid") is True:
                command = result.get("command", "")
                if not isinstance(command, str) or not command.startswith("/workflow start "):
                    return _deny("Invalid draft command")
                try:
                    definition = _definition(
                        command.removeprefix("/workflow start "), session_id, **execution
                    )
                except (ValueError, TypeError) as exc:
                    return _deny(str(exc))
                if state.get("requested_workflow") not in (None, definition["workflow"]):
                    return _deny("Draft must use the workflow selected by the button")
                state["draft"] = {
                    "token": uuid.uuid4().hex,
                    "command": "/workflow start " + json.dumps(definition),
                    "definition": definition,
                }
            return _result(state)
        if target == "workflow_status":
            if _pending(state) and state["status"] == "running":
                _reconcile(state, session_id)
            status = dict(state)
            if state["status"] == "idle":
                status["next_action"] = (
                    "Prepare the start request with workflow_draft. It validates the command "
                    "without starting work. Required: workflow, absolute repository path, task, "
                    "baseline (revision preceding the changes), base (intended PR target branch). "
                    "Ask for missing details; do not invent branch names or review scope. "
                    "Default publish to false unless the user explicitly authorizes publication. "
                    "Read linked context with sys_session_get_history and Linear issues with "
                    "mcp__codex_apps__linear_get_issue. Pass a requested implementation branch "
                    "as workflow_draft branch, not only in task text. GitHub read tools can read "
                    "PR metadata and source. For a Review PR, call workflow_review_checkout with "
                    "the local repository and PR URL; it finds or creates the PR worktree and "
                    "returns repository, branch, baseline and base for workflow_draft. Use the "
                    "PR merge target and Git merge-base; repository conventions are review "
                    "criteria in task, not a Git baseline. This controller supplies "
                    "the command format; do not load the separate omnigent-workflow skill or "
                    "inspect files with shell commands. Summarize the validated task, repository, "
                    "baseline, target branch, candidate branch and publication permission. "
                    "Ask the user to click Start workflow; do not ask them to copy JSON. "
                    "The button approves only the current saved draft. Text clients may send "
                    "/workflow start-draft followed by the saved draft token. "
                    "No implementation starts during drafting."
                )
            return _result(state, data=json.dumps(status, sort_keys=True))
        if target == "workflow_present" and state["status"] in {
            "waiting-design-selection",
            "waiting-final-selection",
        }:
            presentation = comparison.render(state)
            state["presented"] = (
                "designs" if state["status"] == "waiting-design-selection" else "results"
            )
            return _result(state, data=presentation)
        if target == "workflow_reconcile":
            return _reconcile(state, session_id)
        result = _decode(event)
        if target == "workflow_prepare" and state["status"] == "prepare":
            if (
                not result.get("candidates")
                or result.get("run_id") != state["definition"]["run_id"]
            ):
                return _block(
                    state, "Worktree preparation failed; inspect tool error before retry"
                )
            if comparison.enabled(state):
                expected = {c["id"]: c["task"] for c in state["definition"]["candidates"]}
                prepared = result["candidates"]
                if (
                    not isinstance(prepared, list)
                    or len(prepared) != len(expected)
                    or any(
                        not isinstance(c, dict)
                        or not isinstance(c.get("id"), str)
                        or expected.get(c.get("id")) != c.get("task")
                        for c in prepared
                    )
                    or {c["id"] for c in prepared} != set(expected)
                ):
                    return _block(state, "Prepared candidates differ from approved designs")
            state["candidates"] = result["candidates"]
            state["pr_template"] = result.get("pr_template", "")
            _advance_candidate(state)
            return _result(state)
        if target == "workflow_snapshot" and state["status"] == "snapshot":
            try:
                return _accept_snapshot(state, result)
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
        return _accept_worker_result(state, pending, result, metadata, args["conversation_id"])

    return evaluate


def make_controller(
    max_attempts: int = 2, worker_defaults: Json | None = None, stages: Json | None = None
) -> Callable[[PolicyEvent], PolicyResponse]:
    """Build the persisted coding workflow controller."""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    return _coding_controller(max_attempts, _execution_settings({}, worker_defaults, stages))
