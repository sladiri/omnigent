"""Real Codex collector experiment; all inference and state stay in scratch paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from real_cli_support import MARKER, Codex, MockResponses, config

from omnigent.harnesses.codex_native.memory_collector_prototype import (
    import_rollouts,
    prepare_reader,
)


async def run(root, binary):
    root.mkdir(parents=True, exist_ok=True)
    mock = MockResponses(root)
    workspace = root / "workspace"
    workspace.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "home"),
        "MEMORY_TEST_KEY": "fixture",
        "RUST_LOG": "codex_memories_write=debug",
    }
    Path(env["HOME"]).mkdir()
    clients = []
    try:
        a_home = root / "a"
        a_home.mkdir()
        (a_home / "config.toml").write_text(config(mock.url))
        a = await Codex(binary, a_home, workspace, env).start()
        clients.append(a)
        a_id = await a.thread({"features.memories": False})
        await a.turn(a_id, MARKER)
        await a.close()
        clients.remove(a)
        rollouts = list((a_home / "sessions").rglob(f"*{a_id}.jsonl"))
        assert len(rollouts) == 1, rollouts
        original = rollouts[0].read_bytes()
        collector_home = root / "collector"
        collector_home.mkdir()
        (collector_home / "config.toml").write_text(config(mock.url))
        collector = await Codex(binary, collector_home, workspace, env).start()
        clients.append(collector)
        existing = await collector.thread({"features.memories": False})
        await collector.turn(existing, "Initialize a durable collector index.")
        await collector.close()
        clients.remove(collector)
        collector = await Codex(binary, collector_home, workspace, env).start()
        clients.append(collector)
        await import_rollouts(collector, rollouts, collector_home)
        print("Imported source through real Codex resume API", flush=True)
        with sqlite3.connect(collector_home / "state_5.sqlite") as db:
            stamp = int(time.time()) - 7 * 3600
            db.execute(
                "update threads set updated_at=?,updated_at_ms=? where id=?",
                (stamp, stamp * 1000, a_id),
            )
        worker = await collector.thread()
        await collector.turn(worker, "Start the collector fixture task.")
        summary = collector_home / "memories_v2" / "memory_summary.md"
        for _ in range(600):
            if summary.exists() and MARKER in summary.read_text():
                break
            if mock.errors:
                raise RuntimeError(mock.errors)
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("Collector did not generate memory")
        for _ in range(100):
            with sqlite3.connect(collector_home / "memories_v2_1.sqlite") as db:
                selected = db.execute(
                    "select selected_for_phase2 from stage1_outputs where thread_id=?", (a_id,)
                ).fetchone()
            if selected and selected[0]:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Consolidation did not consume the imported thread")
        assert mock.extractions and mock.writes
        reader_home = root / "reader"
        reader_home.mkdir()
        (reader_home / "config.toml").write_text(config(mock.url))
        instructions = prepare_reader(reader_home, collector_home, "Keep existing project rules.")
        reader = await Codex(binary, reader_home, workspace, env).start()
        clients.append(reader)
        thread = await reader.thread({"developer_instructions": instructions})
        before = len(mock.requests)
        prompt = "What do you recall about the fixture development server?"
        await reader.turn(thread, prompt)
        foreground = [
            r for r in mock.requests[before:] if prompt in json.dumps(r.get("input", []))
        ]
        assert len(foreground) == 1, foreground
        assert MARKER in json.dumps(foreground[0].get("input", []))
        assert "Keep existing project rules." in json.dumps(foreground[0].get("input", []))
        assert mock.extractions == 1, "Reader must not trigger native extraction"
        for home in (a_home, reader_home):
            with sqlite3.connect(home / "state_5.sqlite") as db:
                ids = {row[0] for row in db.execute("select id from threads")}
            if home == reader_home:
                assert a_id not in ids
            else:
                assert worker not in ids and thread not in ids
        assert rollouts[0].read_bytes() == original
        result = {
            "variant": "collector",
            "extractions": mock.extractions,
            "writes": mock.writes,
            "native_consolidation_validated": True,
            "private_indexes_isolated": True,
            "reader_received_generated_memory": True,
            "original_source_unchanged": True,
            "artifacts": str(root),
        }
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        for client in clients:
            await client.close()
        mock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--codex-bin", default="/home/sladi/.local/bin/codex")
    args = parser.parse_args()
    root = args.artifacts or Path(tempfile.mkdtemp(prefix="omnigent-collector-"))
    print("Artifacts:", root, flush=True)
    asyncio.run(run(root, str(Path(args.codex_bin).resolve())))
