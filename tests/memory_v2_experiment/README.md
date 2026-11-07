# Memory v2 runtime experiment

This is an isolated experiment, not a deployed migration. It runs the installed
Codex app-server binary against a local deterministic Responses API. It supplies
no real credentials and writes only to a newly created scratch directory.

From this worktree in `dev`, use a Python environment containing Omnigent's
dependencies (the existing tool environment is sufficient):

```fish
/usr/bin/env PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 \
    /home/sladi/.local/share/uv/tools/omnigent/bin/python -B \
    tests/memory_v2_experiment/real_cli.py --mode shared-state --concurrent
```

The command prints the artifact directory and a JSON result. Pass `--codex-bin`
to select another binary or `--artifacts` to select a new artifact directory.
The shared-state experiment requires Linux and `bwrap`.

## Evidence and controls

The harness creates conversation A through the real app-server, then changes
only A's scratch database timestamp to make it eligible without waiting hours.
A's recording turn has memory processing disabled so it cannot generate an
empty baseline before the experiment starts. B and B2 start concurrently.

The checks require:

1. Codex's extraction request contains A's recorded fact.
2. Actual memory extraction stores one stage-1 output.
3. The consolidation agent executes a tool to read generated rollout summaries.
4. The mock returns a write call only after observing the fact in that tool's
   output. Codex executes that write; the harness never seeds the final summary.
5. Codex validates the summary and marks A consumed by phase 2.
6. Exactly one extraction and one write occur despite concurrent B/B2.
7. A separate C foreground model request contains the learned fact.
8. C's process can stop and cold-resume the same thread with the shared mapping.

`--mode native-control --concurrent` runs those pipeline checks in one normal
Codex home, without Omnigent mapping, except the candidate cold-resume check.

`--mode isolated` reproduces separate session indexes. Its expected result is
zero extractions and no learned fact in the generated summary.

`--mode symlink-control` reproduces the rejected directory-symlink design. Its
expected result is zero extractions and Codex's explicit rejection of a
symbolic-link memory root. Expected rejection controls exit successfully only
when the observed failure matches the claim.

## Observed results

On 2026-10-29 with Codex 0.165.0:

| Mode | Result |
| --- | --- |
| Isolated homes | Confirmed failure to extract A |
| Directory symlink | Confirmed runtime rejection before extraction |
| Normal shared-home control, concurrent | Passed; one extraction and write |
| Bind-mounted shared state, concurrent | Passed; one extraction and write, C recall and cold resume |

This verifies storage, coordination, tool execution, and context injection.
Inference is mocked, so it does not measure the quality of a real model's
selection or summaries. It does not migrate existing private session databases,
exercise the complete Omnigent UI, or test long-term rollout retention. Full
request bodies, app-server events, stderr, scratch databases, and `result.json`
remain in the printed temporary directory for inspection.
