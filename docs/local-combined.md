# Local combined release

This branch ports selected local features to the latest stable upstream release
available on 2026-11-07. It keeps the custom workflows and quick composer controls
from the previous combined branch, with their required backend support.

## Base and scope

| Role | Revision |
| --- | --- |
| New branch | `local/combined-v0.21.0` |
| Release base | `v0.21.0`, `2f636330cc6e28bfe1d22f741eda6db34a192d39` |
| Source of local changes | `local/combined-v0.18.0.dev0`, `f601c6aa5132e3944ccba8d25d5a8562f71dfd52` |
| Source branch's upstream base | `7a968cec25543061f58fe1ec41979febd6af1199` |

The release was selected through GitHub's latest stable release record. The
earlier decision report compared upstream main at `0.22.0.dev0`; that development
snapshot is not this branch's base.

Local changes come only from the previous combined snapshot. Existing feature
branch tips may contain later experiments or fixes. Those extra changes are out
of scope, even when their branch names match a retained feature. The previous
branches and worktrees remain available as references.

One combined branch avoids overlapping rebases of the shared composer, runner,
and policy files. The feature groups below provide the maintenance boundaries.

## Retained features

| Group | Behavior | Main source |
| --- | --- | --- |
| Custom workflows | Implement, Compare, and Review; persisted stages; worker reports; user decisions; stage-specific model and effort | `omnigent/workflows/`, `examples/workflow_controller/` |
| Workflow UI | Preparation snippets, saved-draft inspection, Start, Answer, and visible controller selection | Composer components and workflow-draft route |
| Focus | Insert the existing fixed prompt into an existing chat without sending | `ChatPage.tsx` |
| Quick Codex models | Med selects `gpt-6.1-sol`; Hi selects `gpt-6-astra`; neither sends a message | `CodexModelChoice.tsx` |
| Quick effort submission | Supported Low, Medium, High, and X-high choices set effort and send once | `EffortSubmitButtons.tsx`, both composers |
| Project isolation | Keep draft text while applying the target project's launch settings | `NewChatDialog.tsx` |
| Workflow infrastructure | Trusted root policy identity, bundled native tools, instruction protection, worker workspace validation, directory policy, and restored inbox/task state after reconnect | Harness, runner, tool, and policy modules |
| Large local-tool results | Drain the response pipe while its process runs; reject oversized results clearly | `omnigent/tools/local.py` |
| Database compatibility | Preserve the four historical local revisions and join them to the release migration head | Database migrations and upgrade checks |

The model buttons use the normal model-selection path. That path can also clear
an incompatible effort and disable routing. The effort buttons send the draft;
they are not selection-only controls. The Focus text still asks for a minimal
implementation without tests or documentation. It does not disable workflow
policy checks.

Med and Hi appear only when both models resolve uniquely in the active catalog.
A catalog without those two models keeps the ordinary model picker.

Three review roles check each candidate. Routine defects advance to fixes,
followed by targeted rechecks of affected reviews. Graph-only updates retain
review evidence; publication still checks the full snapshot. Questions,
incomplete outcomes, and approval needs stop dependent work. GitHub reads
support both connected-app aliases and the configured GitHub MCP server.
The controller owns stage advancement and publication gates.

## Replaced or omitted changes

- Use the release's independent-child wake and recovery handling instead of the
  duplicate local wake patch.
- Use the release's model-selection and wake-feedback flow instead of the old
  offline-confirmation shortcut.
- Omit the local asynchronous Codex question adapter. Standard elicitation and
  the workflow's Answer control remain separate supported paths.
- Omit the extra `OMNIGENT_RUNNER_ENV_PASSTHROUGH` forwarding into native Codex.
- Keep the release's UI layout and picker helpers. Main-only phone layout changes
  and other later upstream commits are not backported.
- Do not restore the canceled hook-review feature. Its migration history remains
  solely for database compatibility.
- Do not import table-copy experiments, memory experiments, or newer changes that
  exist only on separate feature branches.

## Integration checks

Checks and their results are recorded in the Beads session journal
`omnigent-4y8`. The following checks passed with retries disabled:

| Check | Result |
| --- | --- |
| Backend policy, tool, relay, and harness checks | 1,252 passed initially; all nine initial failures covered by a clean 400-case rerun |
| Runner dispatch after the reconnect repair | 332 passed, one optional case skipped |
| Migration and local-tool unit checks | 108 passed; 16 configured-database cases skipped |
| Complete workflow and approval/restart journeys | Eight passed |
| Workflow Git and review routing journeys | Three passed |
| Workflow GitHub, journal, repository, skills, and native wake boundaries | Six cases passed across focused runs |
| Real Codex instruction override and resume | One passed against a local model stub |
| Concurrent large local-tool responses and restart | One passed |
| Frontend component and page checks | 1,344 passed |
| Built workflow UI at phone and desktop widths | Four passed |
| Production web build and TypeScript checks | Passed |

The initial failures exposed obsolete fixtures, scratch-path constraints, and
the missing reconnect state. Their corrected cases were rerun. Python type
checking also passed after installing the release's optional tracing dependencies
and disabling parent-directory ignore-file discovery for the local check.

No live database upgrade or deployment is part of this reconstruction. The
configured-database matrix remains untested without those database URLs.

To confirm scope, compare this branch with the release:

```fish
git --no-pager diff --stat v0.21.0...local/combined-v0.21.0
git --no-pager log --oneline v0.21.0..local/combined-v0.21.0
```

For human verification after starting the new build in an isolated environment:

1. Select Workflow-controller. Click each workflow snippet and confirm it
   preserves the task without sending. Send the task, inspect the prepared
   details, then click Start.
2. Run a small Implement and Review workflow. Confirm an independent worker's
   completion wakes the controller. Exercise a question through Answer. Confirm
   Compare waits for the required design and final choices.
3. Click Focus in an existing chat. Confirm it inserts the unchanged snippet and
   preserves the draft without sending.
4. In new and existing Codex chats, choose Med and Hi. Confirm model selection
   changes without sending. Use an effort button and confirm one message is sent
   with the selected supported effort.
5. Switch projects with a parked draft. Confirm the text survives and the target
   project's settings take priority.
6. Use a narrow viewport. Confirm workflow controls, model controls, and Send
   remain reachable within the release's layout.

Test any existing database upgrade against a disposable copy before using this
branch with that database. The reconstruction does not modify a live database.
