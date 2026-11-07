# Omnigent memory v2: isolated solution comparison

Date: 2026-10-29. Baseline: installed Omnigent `d342932df` and Codex 0.165.0.
These are competing experimental branches. Neither is installed or enabled.

## Recommendation

For this Linux work machine, prefer **shared state with a child-only bind mount**.
It keeps native Codex memory generation and use automatic while preserving
session-specific configuration. The user does not require separate conversation
indexes. The collector is useful if preserving that separation is more important
than hands-off operation.

Neither prototype is a finished migration for the current installation. Choose a
design before completing migration, retention, and full application validation.

## Compared implementations

| Property | Shared state | Collector |
| --- | --- | --- |
| Branch | `experiment/memory-v2-shared-state` | `experiment/memory-v2-collector` |
| Learning | Native Codex background pipeline after eligibility checks | Explicit collection of completed conversation snapshots |
| Recall | Native Codex memory read path | Published summary injected at reader startup |
| Conversation indexes | Shared `sqlite_home`, including non-memory Codex SQLite state | Separate reader indexes and collector index |
| Model/config isolation | Per-session config retained | Per-session config retained |
| Added runtime requirement | Linux with working bubblewrap/user namespaces | No bind mount; tested on Linux |
| Existing conversations | Refuses private DB migration; use new sessions for the experiment | Can import tested canonical JSONL snapshots through experimental resume API |
| Retention | Retains contributing bridge homes past normal orphan GC | Retains copied snapshots and collector artifacts |
| Main cost | Broader shared state, Linux dependency, retained disk use | Manual collection, snapshot copies, custom recall, no native reader usage telemetry |

Worktrees:

- `/home/sladi/repos/omnigent-memory-v2-shared-state`
- `/home/sladi/repos/omnigent-memory-v2-collector`

The shared candidate mounts only the v2 artifact directory in each Codex child
process's mount namespace. It does not change the running Distrobox mount tree.
It also shares the sibling workspace lock and SQLite database/leases. Config
refresh changes memory settings and database selection without replacing the
session's model or instructions. Disabling memories retains database selection
so existing shared-state conversations can still resume. Managed launch settings
are appended after caller overrides, so those overrides cannot split the database
from its memory artifacts.

The collector uses one writer and disables native memory in readers. Setting
`generate_memories=false` alone would not stop reader background writes. Reader
summaries are bounded to 16,000 characters and treated as fallible evidence.
Collection scheduling, general pagination import, pruning, and a finished user
command remain outside this prototype. The acceptance driver exercises the
collector functions explicitly. The reader opt-in survives the normal Omnigent
environment filter. Readers refresh only at app-server startup, after collection
has finished. Removing the opt-in does not restore an existing reader's private
config; use a new home or restore its memory and SQLite settings deliberately.

## Verification

All model responses were provided by a local deterministic fixture. No live
credentials or production inference were used. The real Codex executable handled
transcript recording, extraction, consolidation tool execution, SQLite state,
and later prompt construction. Tests adjusted only scratch thread timestamps to
avoid waiting for the normal idle window.

Both candidates passed this chain:

1. Conversation A records a distinctive project fact.
2. Codex receives that fact in its extraction input and stores stage-one output.
3. Its consolidation agent executes a tool to read the extracted summary.
4. Only after that read returns the fact does the mock request a memory write.
5. Codex executes the write and marks the source consumed by consolidation.
6. An independent reader's foreground model request includes the learned fact.

Additional shared-state checks passed concurrent B/B2 writers with one extraction
and one write, shared workspace locking, preservation/refusal checks, and cold
resume of the later conversation. The collector check passed import into an
already initialized collector index while preserving the source bytes and
separate reader indexes.

Targeted Python checks passed: shared-state memory/startup checks (22 tests),
the earlier startup/profile/process-registry/bridge regression run (98 tests),
and collector integration plus environment/hooks checks (64 tests). The
shared-state counts overlap in startup coverage. Ruff and Git whitespace checks
passed for both implementations. Final independent review confirmed that the
launch-override and environment-filter fixes cover the integration findings.

Controls:

- Existing isolated homes: zero extraction from the other home, as expected.
- Directory symlink: rejected by Codex before extraction. Native v2 refuses a
  symbolic-link memory root; adding v2 to Omnigent's symlink list is insufficient.
- One ordinary shared Codex home: the complete pipeline passes.

Raw evidence (temporary directories; do not commit them):

- Shared-state acceptance: `/tmp/omnigent-memory-v2-m2hydj0c/result.json`
- Collector acceptance: `/tmp/omnigent-collector-i6k68fyt/result.json`
- Isolated control: `/tmp/omnigent-memory-v2-tta7oo6j/result.json`
- Symlink control: `/tmp/omnigent-memory-v2-lrlq1nb3/result.json`
- Native control: `/tmp/omnigent-memory-v2-m3o9aphc/result.json`

Run each worktree's `tests/memory_v2_experiment/README.md` commands in Distrobox
`dev`. Each run creates fresh scratch data and prints its evidence directory.

## Limits of the evidence

These tests prove the memory data flow, not a real model's judgment or summary
quality. They do not prove the complete Omnigent UI lifecycle, automatic migration
of existing conversations, long-term cleanup, or support for arbitrary future
Codex formats. Shared-state bridge retention currently trades disk growth for
preserving evidence; explicit session deletion needs separate retention policy.
The collector is a manual alternative, not equivalent automatic operation.
Multiple importers are unsupported. A reader started during consolidation can
capture a partial summary. The tested sequence imports completed snapshots,
waits for consolidation, then starts readers. Imports read the whole source into
memory; large transcripts and crash recovery are not covered.

## Ready-to-copy commit messages

Keep these alternatives on separate branches. Do not combine them.

```text
omnigent-xhc feat(codex): prototype shared memory v2

Share Codex state and bind-mount memory artifacts while keeping configs
session-specific. Preserve workspace locking and rollout evidence.
Pin managed launch settings after caller overrides.
Verify cross-session memory with real Codex and local mocked inference.
```

```text
omnigent-xhc feat(codex): prototype a memory collector

Collect completed rollout snapshots with native memory v2 and publish
bounded recall context to readers with separate conversation indexes.
Pass the reader opt-in through Omnigent's native environment filter.
Verify extraction, consolidation, and reader context with local inference.
```
