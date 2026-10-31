# Coding workflow controller

This custom agent implements candidates in separate Git worktrees, runs three
review/fix passes, waits for your selection, then publishes only the selected
result. The controller is a packaged Python policy in `omnigent.workflows`.
Omnigent supplies sessions, durable policy state, child events, and recovery.

The coordinator explains progress and calls the next permitted tool. The policy
supplies worktree paths and worker briefs, records dispatch intent, validates
completion reports, and enforces stage order. Workers cannot advance the run or
publish their changes. Each worker starts with its candidate worktree as the
actual runtime working directory.

Each worker starts a native Codex goal for its assigned stage report. It reads the
full task and prior evidence through `workflow_stage`, so long briefs do not
exceed the native goal objective limit. Codex continues work until the report is
ready. The goal ends with an honest `COMPLETE`, `NEEDS_INPUT`, or `BLOCKED` report;
only the controller decides whether that report passes the stage gate. A missing
user decision stops dependent work and leaves the controller waiting for an
answer. Answers and retries start a fresh worker goal for the same stage.

## Start a run

In the Web UI, select the installed `workflow-controller` agent. Use `implement`
for one approach or `compare` for alternatives, then describe your task. The
buttons prepare the workflow start message. Review and send that message to start
the run. Worker goal mode is automatic for both workflows; no goal toggle or
separate controller process is needed.

To launch the packaged agent from a checkout instead:

```sh
/usr/bin/env uv run --no-sync omnigent run examples/workflow_controller --server http://127.0.0.1:6767
```

The default coordinator and worker harness is `codex-native`. Configure that
harness normally before launching. The server and local runner must both use an
Omnigent installation containing `omnigent.workflows`.

Send one command with a JSON definition. Replace the repository, task, and
candidate descriptions with the actual work. The repository must be clean so
candidate ownership is unambiguous. `baseline` fixes the comparison starting
point; `base` is the intended PR target branch.

```text
/workflow start {"repository":"/absolute/path/to/repository","task":"Implement the requested behavior and run its acceptance checks.","baseline":"main","base":"main","candidates":[{"id":"simple","task":"Use the simplest existing helper."},{"id":"alternative","task":"Implement the agreed alternative."}],"publish":true}
```

Use a single candidate when the implementation approach is already clear. The
controller still requires explicit selection before publication. `publish: true`
authorizes the final signed commit, branch push, and PR creation after selection.
Use `publish: false` to stop with the selected reviewed worktree. Starting a run
does not authorize merging or deployment.

Preparation creates dedicated candidate branches and worktrees. It records
ownership under the repository's common Git directory in `omnigent-workflows/`.
It reuses worktrees already owned by the same run and preserves other worktrees.
Candidate entries can specify `branch` and `workspace` before the run begins.
Existing unrelated branches or paths are refused rather than taken over.

If `~/bin/setup-worktree.py` exists, preparation invokes it for the candidate
worktree. Set `worktree_setup` to an absolute helper path when needed. An explicit
missing helper is an error. `worktree_setup: false` is for disposable test repos;
it must not override repository instructions requiring local setup.

## Stages and evidence

Each candidate completes this sequence:

```text
implement
review-a -> fix-a -> verify-a
(review-b-standards + review-b-spec) -> fix-b -> (verify-b-standards + verify-b-spec)
review-c -> fix-c -> verify-c
final-b-standards + final-b-spec
```

A and C use the installed `ponytail` and `ponytail-review` skills. B uses the
installed `code-review` skill. Its Standards and Spec subreviews must remain
independent. The controller dispatches each pair in parallel to separate fresh
worker sessions and waits for both reports. Missing required skills block the
stage. Reviews inspect the complete
working-tree change and connected code against the saved baseline. They do not
require an intermediate commit.

Each fix accounts for every outstanding finding. Its verification stage checks
the resulting code. `final-b` rechecks Standards and Spec after the final C fixes.
Unresolved verification findings loop back to their fix stage, subject to the
configured attempt limit. A successful turn or an idle worker is not a passed
gate: the controller requires the expected structured evidence, including explicit
`findings` and `resolved` lists on every `COMPLETE` report.

Content snapshots include tracked, untracked, staged, and unstaged working files
and executable bits. A changed revision invalidates affected review evidence.
The selected revision is checked again before publication. Snapshots detect file
changes; review quality still depends on the reviewers and their recorded checks.

## Questions, selection, and recovery

When a worker needs a decision, the coordinator presents its question and waits.
Reply with `/workflow answer <answer>`. Unrelated messages and ordinary runtime
wakes do not answer it. `/workflow retry` retries a blocked terminal stage after
you resolve its blocker. Fix attempts are bounded by `max_attempts` in config.
To abandon a blocked candidate, send `/workflow reject <id> <reason>`. The
controller preserves its worktree and advances to the next candidate. Rejected
candidates cannot be selected.

After all candidates pass, compare their evidence and send an exact selection:

```text
/workflow select simple
```

The other candidate worktrees remain available. The controller does not copy the
selected files into the original checkout.

Resume the original coordinator session after interruption. `workflow_status`
reads its durable state. Failed or cancelled workers can be retried after their
terminal event and stopped state are confirmed. `workflow_reconcile` looks for the child belonging to an
uncertain dispatch before another send is allowed. A recovered child is observed
through its existing inbox; it is not dispatched again. If no child exists,
inspect and stop any old in-flight dispatch before explicitly authorizing
`/workflow retry-dispatch`. This avoids competing writers when the original send
has an unknown outcome.

Publication runs on the local host. It preserves signing settings, obtains the
active graphical pinentry environment, and allows Git's hardware-key interaction
to wait. The operation saves publication progress and checks local HEAD, the pinned push
destination, and existing PRs before repeating work after interruption. Closed or
merged PRs never cause their deleted branches to be recreated. Do not
start a second publication process while the first is waiting for your key.

The coordinator supplies commit/PR text from the final diff. The host operation
checks the repository PR template, stages the owned candidate files, and runs
pre-commit before publication. New files are included in these checks.
The candidate must have the repository's development prerequisites installed,
including `.venv/bin/pre-commit` or `pre-commit` on PATH. Publication failures leave
the run blocked; fix the cause, then retry the same run. Native approval prompts
must be answered through their native UI.

## Verify in isolation

Run in the repository's development environment:

```sh
/usr/bin/env uv run --no-sync pytest tests/policies/test_workflow_controller.py tests/tools/test_workflow_operations.py tests/e2e/test_workflow_controller_e2e.py -v
```

The tests use temporary repositories, a real isolated server and runner, and a
scripted model endpoint. They do not call a model provider or change the running
Omnigent installation. The coding end-to-end test creates a candidate worktree,
executes worker file tools, checks reviewer and out-of-worktree write denial,
reads the assigned goal brief, resumes a question after restart, completes the parallel review pairs, restarts
the runner again, and requires selection before publication. Only
the external publication effect is replaced in that test; operation tests cover
Git state and mocked GitHub interactions separately.

For a manual check, run a small change in a disposable repository. Confirm that
the original checkout stays unchanged, direct reviewer file writes are denied,
and each review leaves the content snapshot unchanged. Confirm that no commit
or PR exists before selection and only the selected candidate is published.
Restart the isolated runner while waiting for selection and verify the evidence
and selected state remain intact.

## Runtime contract and scope

Delayed child-result policies receive `event.request_data.subagent` containing
terminal `status` and `work_id`. Failed and cancelled turns cannot pass with
success-looking text. Delivery IDs, deterministic dispatch titles, and saved
child IDs prevent duplicate or stale completion events from advancing the run.
The trusted `root_conversation_id` identifies inherited worker policy calls.
The controller also supplies a trusted `workspace` on each named child creation.
Omnigent binds the child's runtime and OS tools to that directory before its
first turn. A continuation cannot change an existing child's workspace.
Worker briefs supply an explicit `cd` prefix for native shell calls because native
hooks omit `workdir`. Workers edit through `sys_os_write` and `sys_os_edit`;
native `apply_patch` is not supported by this controller.

Workers run as the local user with `os_env.sandbox.type: none`, as native Codex
does. Policy guards constrain declared paths, deny reviewer write tools, and
reject publication commands. They are workflow controls, not a security sandbox
for arbitrary shell code. Use a trusted local repository and runner.

This controller targets one local server and its runner. It does not provide a
multi-server distributed lock. Native Codex reasoning and review quality require
manual provider-backed verification; deterministic tests validate the tool and
state transitions. Keep the coordinator session and candidate worktrees until
publication and its evidence are complete.
