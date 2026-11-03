# Coding workflow controller

This custom agent implements candidates in separate Git worktrees, runs three
review/fix passes, waits for your selection, then publishes only the selected
result. Review uses the existing implementation in its current worktree and skips
the implementation stage. Compare first explores designs without implementation
and waits for your approval. After implementation, it presents a second comparison from recorded
results and waits for a separate final selection. The controller is a packaged
Python policy in `omnigent.workflows`.
Omnigent supplies sessions, durable policy state, child events, and recovery.

The coordinator explains progress and calls the next permitted tool. The policy
supplies worktree paths and worker briefs, records dispatch intent, validates
completion reports, and enforces stage order. Workers cannot advance the run or
publish their changes. Each worker starts with its candidate worktree as the
actual runtime working directory.

Existing implementation, review, and fix workers start a native Codex goal for
their assigned stage report. Exploration and result comparison do not start goals.
Each worker reads the full task and prior evidence through `workflow_stage`, so
long briefs do not exceed the native goal objective limit. Codex continues work until the report is
ready. The goal ends with an honest `COMPLETE`, `NEEDS_INPUT`, or `BLOCKED` report;
only the controller decides whether that report passes the stage gate. A missing
user decision stops dependent work and leaves the controller waiting for an
answer. Answers and retries start a fresh worker for the same stage.

## Start a run

In the Web UI, select the installed `workflow-controller` agent. Use `implement`
for one approach, `compare` for alternatives, or `review` for an existing
implementation. Describe your task and send it. Answer any missing-input questions,
review the prepared details, then click **Start workflow**. No JSON copying or
separate controller process is needed.

For Review, select the project/worktree containing your implementation, click
`review`, and enter, for example: "Review my current changes against main."
The controller reviews committed and uncommitted task changes in that worktree.
If the scope or baseline is unclear, it asks before starting. Findings wait for
your decision before fixes. Approved fixes remain in the same worktree.
Review defaults to no publication unless you request it.

For Compare, the coordinator shows stable design IDs, expected benefits and
disadvantages, complexity, maintenance cost, risks, assumptions, open questions,
and a reasoned recommendation. These are expectations, not implementation results.
There is no fixed candidate count. Even one design needs your explicit approval.

Use the revision number shown in the comparison. For revision 1:

- `/workflow designs 1 all` approves every remaining design for implementation.
- `/workflow designs 1 select simple cache` approves only those IDs.
- `/workflow designs 1 reject cache` rejects that proposal without starting work.
- `/workflow designs 1 revise Avoid a new dependency` requests revised proposals.
- `/workflow cancel` cancels at a stopped decision gate and preserves the record.

After a rejection, the coordinator presents the remaining choices again. A
revision creates a new approval number; old approvals cannot authorize it. A
material design change discovered during implementation stops that candidate for
new proposals and approval. `/workflow answer` cannot approve a design change.
Only approved designs receive isolated worktrees and the existing review stages.
Earlier proposal rounds, decisions, evidence, and worktrees remain available.

When implementation and review finish, the coordinator presents actual behavior,
recorded test and review evidence, tradeoffs, limitations, rejected or unsuccessful
candidates, and a recommendation. Send `/workflow select simple` only after this
second comparison to make the final choice. Implementation approval is not final
selection and does not add publication permission. Ordinary messages, runtime
wakes, goal continuations, and elapsed time never approve either gate.

To launch the packaged agent from a checkout instead:

```sh
/usr/bin/env uv run --no-sync omnigent run examples/workflow_controller --server http://127.0.0.1:6767
```

The default coordinator and worker harness is `codex-native`. Configure that
harness normally before launching. The server and local runner must both use an
Omnigent installation containing `omnigent.workflows`.

The coordinator prepares the start command for you. For direct API/CLI use,
send a JSON definition with the repository and task. Implement and Compare need
a clean repository so candidate ownership is unambiguous. Review accepts the
existing worktree, including uncommitted changes. `baseline` fixes the comparison
starting point; `base` is the intended PR target branch.

```text
/workflow start {"workflow":"compare-review-fix","repository":"/absolute/path/to/repository","task":"Implement the requested behavior and run its acceptance checks.","baseline":"main","base":"main","candidates":[{"id":"simple","task":"Use the simplest existing helper."},{"id":"alternative","task":"Implement the agreed alternative."}],"publish":true}
```

For Compare, `candidates` are optional initial suggestions, never approval.
Use `workflow: implement-review-fix` with a candidate when the implementation
approach is already clear. It retains the existing review and selection behavior.
Use `workflow: review-fix` with exactly one candidate to review the existing
implementation in `repository`. The baseline must precede the changes to review;
preparation preserves the current branch, files, index, and commits. Detached HEAD
is refused because the reviewed branch must remain identifiable across resume.
Each workflow button highlights its choice and replaces earlier workflow prefixes
in the draft. Send prepares the request; it does not start implementation.
The controller calls `workflow_status` for preparation guidance, asks for missing
inputs, and uses `workflow_draft` to validate the command with the same parser
used at startup. The validated draft is saved across restarts without filesystem
or worker side effects. The Web UI shows its task, repository, baseline, target
branch, candidate branch, and publication permission. **Start workflow** submits
`/workflow start-draft <token>` for that specific draft. New user instructions or
a replacement draft invalidate the previous token. Linear issue and agent-context
reads are allowed while idle; Linear writes remain blocked. Pass an exact requested
branch through `workflow_draft`'s `branch` argument for Implement or Review; Compare
assigns separate branches after design approval. The separate
`omnigent-workflow` skill is not this controller's command reference.
Each button records its choice so the prepared start message cannot silently
switch workflows. Direct definitions with multiple candidates
and no workflow name also select Compare. New Compare runs use these gates;
already-running workflows retain their saved workflow kind. `publish: true`
authorizes the final signed commit, branch push, and PR creation after selection.
Use `publish: false` to stop with the selected reviewed worktree. Starting a run
does not authorize merging or deployment.

Implement and Compare preparation create dedicated candidate branches and
worktrees. Preparation records ownership under the repository's common Git directory in `omnigent-workflows/`.
It reuses worktrees already owned by the same run and preserves other worktrees.
Implement candidate entries can specify `branch` and `workspace` before the run
begins. Compare derives owned paths from the approved proposal revision.
Existing unrelated branches or paths are refused rather than taken over.

If `~/bin/setup-worktree.py` exists, preparation invokes it for the candidate
worktree. Set `worktree_setup` to an absolute helper path when needed. An explicit
missing helper is an error. `worktree_setup: false` is for disposable test repos;
it must not override repository instructions requiring local setup.

## Stages and evidence

Each candidate completes this sequence. Review skips only `implement`:

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
Any review or verification findings stop for your decision before the fix stage.
After your answer, unresolved verification findings loop back to their fix stage,
subject to the configured attempt limit. A successful turn or an idle worker is
not a passed gate: the controller requires structured evidence, including explicit
`findings` and `resolved` lists on every `COMPLETE` report.

Content snapshots include tracked, untracked, staged, and unstaged working files
and executable bits. A changed revision invalidates affected review evidence.
The selected revision is checked again before publication. Snapshots detect file
changes; review quality still depends on the reviewers and their recorded checks.

## Questions, selection, and recovery

All workflows separate review from fixing. Every initial review, verification,
and final review stops for findings, conflicts, questions, or blockers. Clean
reviews continue. The parallel Standards and Spec reviews collect both reports
before asking; no fix or later stage starts while a decision is pending.

The coordinator presents the findings and questions. Reply with
`/workflow answer <decision and instructions>`, for example:
`/workflow answer Keep the existing API and fix the missing validation`.
Completed reviews then proceed to their fix stage. Incomplete or blocked reviews
run again with your answer. Review decisions and evidence survive restart.
A materially different Compare design still requires new proposal approval.

Other workers also wait when they need a decision. Unrelated messages, runtime
wakes, and goal continuations are not answers. `/workflow retry` cannot bypass a
review decision; it only retries a blocked terminal stage after you resolve its
blocker. Fix attempts are bounded by `max_attempts` in config.
To abandon a candidate blocked or waiting at review, send
`/workflow reject <id> <reason>`. The controller preserves its worktree and
advances to the next candidate. Rejected
candidates cannot be selected.

After the result comparison is presented, send an exact final selection:

```text
/workflow select simple
```

The other candidate worktrees remain available. The controller does not copy the
selected files into the original checkout. Review already operates in the
original worktree; its fixes are present there.

Resume the original coordinator session after interruption. `workflow_status`
reads its durable state, including proposals, approval decisions, and both waits.
`workflow_present` renders the saved comparison again after reconnect. Failed or
cancelled workers can be retried after their terminal event and stopped state are
confirmed. `workflow_reconcile` looks for the child belonging to an
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
/usr/bin/env uv run --no-sync pytest tests/policies/test_workflow_controller.py tests/policies/test_workflow_comparison.py tests/tools/test_workflow_operations.py tests/e2e/test_workflow_controller_e2e.py -v
```

The tests use temporary repositories, a real isolated server and runner, and a
scripted model endpoint. They do not call a model provider or change the running
Omnigent installation. The coding end-to-end test creates a candidate worktree,
executes worker file tools, checks reviewer and out-of-worktree write denial,
reads the assigned stage brief, resumes a question after restart, completes the
parallel review pairs, restarts the runner again, and requires selection before
publication. The Review case uses the current dirty worktree, skips implementation,
and exercises the same review waits and recovery. The Compare case also checks
read-only exploration, complete proposal presentation, revision, rejection, subset approval, no early worktree
creation, durable design waiting, and the final result comparison. Policy tests
cover all-design approval, single-design approval, cancellation, unsuccessful
candidates, stale decisions, and invalid evidence. A separate isolated server
restart check reloads both the design wait and the recorded approval from disk. Only
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
Native hooks preserve the execution directory. Shell tools can use their candidate
workdir, its subdirectories, or the explicit `cd` prefix in the brief. Workers can
edit through `sys_os_write`, `sys_os_edit`, or native `apply_patch` with explicit
patch text. The controller checks every patch path, including rename destinations,
against the candidate after resolving relative paths and symlinks. Unrecognized
patch input is denied. Review stages still reject edits. Workers cannot create or
switch to sibling worktrees; the coordinator owns candidate creation.

Workers run as the local user with `os_env.sandbox.type: none`, as native Codex
does. Policy guards constrain declared paths, deny reviewer write tools, and
reject publication commands. They are workflow controls, not a security sandbox
for arbitrary shell code. Use a trusted local repository and runner.

This controller targets one local server and its runner. It does not provide a
multi-server distributed lock. The quality of proposed designs, recommendations,
and native Codex reviews requires manual provider-backed verification; deterministic
tests validate the tool and state transitions. Keep the coordinator session and candidate worktrees until
publication and its evidence are complete.
