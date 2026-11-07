# Isolated memory collector experiment

This branch proves a possible memory architecture with Codex 0.165.0. It does
not enable memories in the installed Omnigent or use live sessions.

Each Omnigent reader keeps its own `CODEX_HOME` and SQLite index. A separate,
durable Codex home owns native v2 extraction and consolidation. Completed
rollouts are copied there, then registered through experimental
`thread/resume.path`. The copy uses Codex's canonical
`sessions/YYYY/MM/DD/rollout-...jsonl` layout. A copy outside that layout failed
with `no rollout found for thread id` for the default paginated history mode.

Readers set `features.memories=false` and pin `sqlite_home` to their private
home. `OMNIGENT_CODEX_MEMORY_COLLECTOR_HOME` opts a reader into loading up to
16,000 characters of the collector's generated `memory_summary.md` at app-server
startup. Existing user and agent instructions remain in place. The added text
marks the memory as fallible evidence and supplies the detailed-memory path.
Native memory generation is disabled for readers because their private indexes
cannot coordinate native consolidation of a shared memory store.

## Reproduce the proof

Run in the `dev` Distrobox from this worktree. These commands use scratch homes,
a local deterministic Responses server, and the installed real Codex binary.
They use no production credentials or remote inference. The repository Python
environment supplies the test dependencies.

```fish
cd /home/sladi/repos/omnigent-memory-v2-collector
/usr/bin/env PYTHONPATH=$PWD:$PWD/sdks/python-client:$PWD/sdks/ui PYTHONDONTWRITEBYTECODE=1 /home/sladi/repos/omnigent/.venv/bin/python -m pytest -q -p no:cacheprovider tests/inner/test_memory_collector_prototype.py tests/harnesses/codex_native/app_server/test_memory_collector.py tests/harnesses/codex_native/app_server/test_startup.py tests/harnesses/codex_native/app_server/test_instructions.py
/usr/bin/env PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 /home/sladi/repos/omnigent/.venv/bin/python tests/memory_v2_experiment/collector.py
```

The proof creates source A, initializes and restarts a durable collector, and
imports A into that existing index. It adjusts only the scratch thread idle
timestamp to avoid a real multi-hour wait. A collector turn then triggers native
extraction. The fake model reads the extracted rollout summary through a real
Codex shell tool before it asks Codex to write the final memory summary. The test
checks native `selected_for_phase2` validation. It then starts independent reader
C and checks C's actual foreground model request for both the learned fact and
its existing project rule. It also checks that A's original bytes are unchanged
and that reader/source indexes do not acquire each other's threads.

The script prints its `/tmp/omnigent-collector-*` artifact directory. `result.json`
records success. `request-*.json`, per-home `events.json`, `stderr.log`, generated
Markdown, and SQLite databases retain the evidence. Memory output is generated
by Codex's consolidation tool execution; the test does not seed that output.

## What remains manual

`memory_collector_prototype.import_rollouts` accepts an initialized app-server
client and an explicit list of completed rollout paths. It copies each source
once and registers it with the collector. It refuses to overwrite an existing
copy. A normal collector turn starts the native background pipeline. This
prototype provides the API and the reproducible test command; it does not add a
production collector command, scheduler, discovery service, or UI.

Only one collector process may import at a time. Callers must select completed,
memory-eligible sources and wait for native generation to finish. Reader
summaries refresh only when their app-server starts. Start readers after
consolidation finishes; concurrent reads can capture a partially written summary.
This is not the user's requested hands-off workflow yet.

Only a complete canonical single JSONL source was tested. Compressed rollouts,
reverted or truncated histories, and separate pagination data are not handled.
No long-running concurrency, crash recovery, retention, refresh of an existing
snapshot, authentication lifecycle, or model quality was proved. Snapshot import
uses an experimental Codex API. Real inference would consume model usage.
Import reads each whole source into memory, so large transcripts remain untested.

The opt-in pins the reader's private config, so removing the environment variable
does not itself restore native memory settings in an existing private home.
Use a new private home or restore its memory/SQLite settings deliberately when
ending the experiment. No such change was made to a live home in this task.

Beads and repository documentation remain the places to publish reviewed shared
knowledge. Generated memories remain local evidence until reviewed and promoted.
