# Workflow extension probe

This runnable custom agent tests whether Omnigent extensions can enforce stage
order and recover child results. It uses two harmless greeting candidates, each
with a proposal and a review, then waits for an explicit selection. It does not
run the full implementation/review/fix/publication workflow.

The controller is a Python function policy. Omnigent supplies its trusted session
identity, persists its state, starts worker sessions, and delivers completion
notifications. The coordinator explains results and calls tools. It cannot pick
the next stage or substitute its own worker brief.

## Run the isolated check

Run these commands in the `dev` Distrobox, from this checkout. They are valid in
fish and Bash. The test creates its own server, runner, database, configuration,
ports, and scripted model endpoint. It does not call a model provider or change
your installed Omnigent instance.

```sh
/usr/bin/env OMNIGENT_SKIP_WEB_UI=true uv sync --frozen --extra all --group dev
/usr/bin/env uv run --no-sync pytest tests/policies/test_workflow_controller.py tests/e2e/test_workflow_controller_e2e.py -v
```

The end-to-end check registers this actual agent bundle. It substitutes a mock
model for the coordinator and worker. It checks dispatch, duplicate rejection,
completion, restart recovery, the selection pause, and persisted selection.
The policy tests rebuild the engine from SQLite after each event. They also check
failure, cancellation, stale deliveries, questions, and bounded retries.

The broader runtime recovery checks are:

```sh
/usr/bin/env uv run --no-sync pytest tests/e2e/test_subagent_autowake_e2e.py::test_subagent_completion_auto_wakes_idle_parent tests/e2e/test_subagent_restart_recovery_e2e.py tests/e2e/test_subagent_wake_loss_e2e.py -v
```

These cover an idle-parent wake, an unread child result after a runner restart,
and a child finishing during a server outage. Each test owns its temporary
processes and cleans them up.

## Agent use

The server must run from this source checkout so that the policy module
`examples.workflow_controller.controller` is importable. This is a development
example, not an independently installable plugin. Register `config.yaml` as a
directory bundle on an isolated server. Its default harness is `codex-native`;
provider credentials and native Codex behavior are outside the deterministic test.

Tell the coordinator to start the probe. Inspect the two candidates and their
review evidence. Confirm that no stage starts while it waits for selection.
Then send exactly one of:

```text
/workflow select alpha
/workflow select beta
```

After selection, ask for status again. It must retain the selected candidate and
all four stage results. Restart the isolated runner and repeat the status check.

If a worker asks a question, reply with `/workflow answer <answer>`. If a terminal
stage fails, `/workflow retry` starts a new attempt for that stage. Attempts are
bounded by `max_attempts`; the limit includes the first dispatch. Unrelated input,
worker text, and standard runtime wakes do not answer a question or select a
candidate.

## What the probe enforces

- A fixed candidate/stage matrix, with a definition fingerprint checked on resume.
- One outstanding stage. The policy saves dispatch intent before allowing the tool.
- A fresh, deterministic child title for each stage attempt.
- Completion only from the matching child, with a successful terminal status,
  a delivery ID, the correct stage ID, and nonempty evidence.
- Duplicate and stale events do not advance the workflow.
- Failed or cancelled turns cannot pass by returning success-looking text.
- All candidates finish before selection. Only an exact request command selects.

`workflow_status` returns the persisted `workflow_controller` state. Its
`completed` map records stage evidence, child IDs, and delivery IDs. `pending`
records the active dispatch. The result contract is:

```json
{"stage":"alpha/propose","outcome":"COMPLETE","evidence":"Hello, world."}
```

`NEEDS_INPUT` and `BLOCKED` also require a nonempty `question`. A report attests to
the worker's checks; the controller does not independently judge review quality.

## Extension boundary

Existing policy state and native inbox recovery provide the stage and resume
mechanisms. The probe exposed one event-contract gap: delayed result policies
could not distinguish a failed turn from a successful one, and cancelled turns
skipped those policies. The runtime change is limited to delivering all terminal
statuses and adding this metadata without changing the existing result or args:

```json
{"subagent":{"status":"completed","work_id":"delivery-id"}}
```

This is `event.request_data.subagent`. Consumers must deduplicate `work_id` because
policy evaluation can succeed before receipt persistence. The controller refuses
completion without a delivery ID, including legacy results that lack one.

Remaining boundaries before a production coding workflow:

- An interrupted dispatch with no known outcome stays blocked. Inspect existing
  child sessions before recovery; this probe has no automatic redispatch or
  reconciliation command. Known unread completions use Omnigent's existing recovery.
- Native wakes reach request policies without a separate human-origin flag. Exact
  commands distinguish ordinary wakes; this is not a general human-authorization
  API for arbitrary automated requests.
- Parent policies also apply to workers. This probe deliberately uses text-only
  workers. A coding workflow needs an explicit worker tool policy and workspace
  assignment before reusing these gates for implementation.
- The current process serializes policy evaluation per session. Multi-server
  atomic dispatch and concurrent coordinator ownership are not tested here.
- Worktree setup, the existing three review/fix passes and their skill prompts,
  changed-code review invalidation, candidate rejection, signing, push/PR recovery,
  and publication are not implemented. This probe ends at selection.

Keep the controller separate from the runtime. Expand the extension only after
the required workflow and recovery paths have concrete checks.
