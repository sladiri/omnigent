"""Runner-side access to the existing repository Beads session journal."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from omnigent.host.git_worktree import list_worktrees
from omnigent.workflows.operations import _git, _run

ACTIONS = frozenset({"instructions", "list", "show", "create", "claim", "comment", "close"})


def workflow_journal(repository: str, action: str, issue: str = "", text: str = "") -> str:
    """Run fixed Beads journal operations without shell or arbitrary database arguments."""
    if action not in ACTIONS:
        raise ValueError("Unknown journal action")
    root = Path(
        _git(Path(repository).expanduser().resolve(strict=True), "rev-parse", "--show-toplevel")
    ).resolve()
    if not (root / ".beads").is_dir():
        root = Path(list_worktrees(repo_path=str(root))[0].path).resolve()
    beads = root / ".beads"
    if not beads.is_dir():
        return json.dumps(
            {"available": False, "reason": "No existing Beads journal; skip journaling"}
        )
    if beads.is_symlink():
        raise ValueError("Beads journal must be local to its checkout")
    if action == "instructions":
        prime = beads / "PRIME.md"
        if not prime.resolve().is_relative_to(beads):
            raise ValueError("Journal instructions must stay in .beads")
        return json.dumps(
            {
                "available": True,
                "repository": str(root),
                "instructions": prime.read_text() if prime.is_file() else "",
            }
        )
    metadata = beads / "metadata.json"
    if not metadata.resolve().is_relative_to(beads):
        raise ValueError("Journal metadata must stay in .beads")
    database = (
        json.loads(metadata.read_text()).get("database", "beads.db")
        if metadata.is_file()
        else "beads.db"
    )
    if not isinstance(database, str) or Path(database).name != database:
        raise ValueError("Journal database must be a local filename")
    db = beads / database
    if not db.is_file() or not db.resolve().is_relative_to(beads):
        raise ValueError("An existing local Beads database is required")
    # Inherited Beads overrides must not select another project's journal.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("BEADS_", "BR_"))
    }

    def run(*args: str) -> str:
        return _run(root, "br", "--db", str(db), "--json", *args, env=env)

    if action == "list":
        return run(
            "list", "--all", "--label", "session", "--label", "agent-journal", "--limit", "50"
        )
    if action == "create":
        if not text.strip().startswith("Session: "):
            raise ValueError("Journal title must start with Session: ")
        return run("create", "--title", text, "--labels", "session,agent-journal")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", issue):
        raise ValueError("Use an explicit Beads journal issue ID")
    record = json.loads(run("show", issue))
    if (
        not isinstance(record, list)
        or len(record) != 1
        or not {"session", "agent-journal"}.issubset(record[0].get("labels", []))
    ):
        raise ValueError("Only session journals labeled session and agent-journal are supported")
    if action == "show":
        return json.dumps(record)
    if action == "claim":
        return run("update", issue, "--claim")
    if not text.strip():
        raise ValueError("Journal comment or close reason must not be empty")
    if action == "comment":
        return run("comments", "add", issue, "--message", text)
    return run("close", issue, "--reason", text)
