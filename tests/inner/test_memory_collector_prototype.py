"""Local-only checks for the manual collector experiment."""

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import tomllib

from omnigent.harnesses.codex_native.memory_collector_prototype import (
    SUMMARY_LIMIT,
    import_rollouts,
    prepare_reader,
    snapshot_rollout,
)


class CollectorTests(unittest.TestCase):
    def test_reader_preserves_instructions_config_and_private_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reader = root / "reader"
            reader.mkdir()
            collector = root / "collector"
            collector.mkdir()
            (reader / "config.toml").write_text(
                'model = "existing"\nsqlite_home = "/shared"\n[features]\nmemories = true\n'
            )
            state = reader / "state_5.sqlite"
            state.write_bytes(b"private-state")
            memory = collector / "memories_v2"
            memory.mkdir()
            summary = "VERIFIED LESSON " + "x" * (SUMMARY_LIMIT + 100)
            (memory / "memory_summary.md").write_text(summary)
            result = prepare_reader(reader, collector, "Existing rule")
            self.assertTrue(result.startswith("Existing rule\n\n"))
            self.assertIn(summary[:SUMMARY_LIMIT], result)
            self.assertNotIn(summary[: SUMMARY_LIMIT + 1], result)
            config = tomllib.loads((reader / "config.toml").read_text())
            self.assertFalse(config["features"]["memories"])
            self.assertEqual(config["model"], "existing")
            self.assertEqual(config["sqlite_home"], str(reader.resolve()))
            self.assertEqual(state.read_bytes(), b"private-state")
            self.assertFalse(state.is_symlink())
            self.assertEqual((memory / "memory_summary.md").read_text(), summary)

    def test_absent_summary_disables_writer_without_inventing_memory(self):
        with TemporaryDirectory() as directory:
            reader = Path(directory) / "reader"
            reader.mkdir()
            self.assertEqual(
                prepare_reader(reader, Path(directory) / "collector", "rules"), "rules"
            )
            with self.assertRaises(ValueError):
                prepare_reader(reader, reader, "rules")

    def test_import_resumes_snapshot_and_does_not_mutate_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            thread_id = str(uuid4())
            source = root / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
            data = json.dumps({"type": "session_meta", "payload": {"id": thread_id}}) + "\n"
            source.write_text(data)
            calls = []

            class Client:
                async def request(self, method, params):
                    calls.append((method, params))

            result = asyncio.run(import_rollouts(Client(), [source], root / "collector"))
            self.assertEqual(result, [thread_id])
            self.assertEqual(calls[0][0], "thread/resume")
            self.assertNotEqual(Path(calls[0][1]["path"]), source)
            self.assertEqual(Path(calls[0][1]["path"]).read_text(), data)
            self.assertEqual(source.read_text(), data)
            self.assertFalse(calls[0][1]["config"]["features.memories"])
            with self.assertRaises(FileExistsError):
                snapshot_rollout(source, root / "collector")
            source.write_text("incomplete JSON")
            with self.assertRaises(ValueError):
                snapshot_rollout(source, root / "collector")


if __name__ == "__main__":
    unittest.main()
