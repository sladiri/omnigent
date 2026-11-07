"""Scratch acceptance test: real Codex runtime, deterministic local inference only."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MARKER = "The nebula fixture project uses port 48731 for its development server."
MODEL = "gpt-5.3-codex"


def assistant(text):
    return {
        "type": "message",
        "role": "assistant",
        "id": "msg_fixture",
        "content": [{"type": "output_text", "text": text}],
    }


class MockResponses:
    def __init__(self, root):
        self.root = root
        self.requests = []
        self.errors = []
        self.extractions = 0
        self.consolidations = 0
        self.writes = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    encoding = self.headers.get("Content-Encoding", "")
                    if encoding == "gzip":
                        raw = gzip.decompress(raw)
                    elif encoding == "zstd":
                        import zstandard

                        raw = zstandard.ZstdDecompressor().decompress(raw)
                    body = json.loads(raw)
                    with owner.lock:
                        owner.requests.append(body)
                        n = len(owner.requests)
                        (owner.root / f"request-{n}.json").write_text(json.dumps(body, indent=2))
                        item = owner.respond(body)
                    response_id = f"resp_{n}"
                    events = [
                        {"type": "response.created", "response": {"id": response_id}},
                        {"type": "response.output_item.done", "item": item},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": response_id,
                                "usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 10,
                                    "total_tokens": 20,
                                },
                            },
                        },
                    ]
                    encoded = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except Exception as exc:
                    owner.errors.append(repr(exc))
                    self.send_error(500, repr(exc))

        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def respond(self, body):
        text = json.dumps(body.get("input", []))
        fmt = body.get("text", {}).get("format", {})
        if "rollout_summary" in json.dumps(fmt):
            assert MARKER in text, "Extraction did not receive the original learned fact"
            self.extractions += 1
            return assistant(
                json.dumps({"rollout_summary": MARKER, "rollout_slug": "nebula-port"})
            )
        if "Consolidate the supplied rollout summaries" in text:
            self.consolidations += 1
            outputs = [x for x in body.get("input", []) if x.get("type") == "function_call_output"]
            if any(x.get("call_id", "").startswith("write_memory_") for x in outputs):
                return assistant("Memory summary written.")
            reads = [x for x in outputs if x.get("call_id", "").startswith("read_memory_")]
            if not reads:
                command = "python3 -c " + shlex.quote(
                    "from pathlib import Path; "
                    'print("\\n".join(p.read_text() '
                    'for p in Path("rollout_summaries").glob("*.md")))'
                )
                call_id = "read_memory_" + str(self.consolidations)
            else:
                supported = MARKER in json.dumps(reads)
                summary = (
                    "v1\n## User Profile\n\n## User preferences\n"
                    "\n## General Tips\n\n## What's in Memory\n"
                )
                if supported:
                    summary += "### Nebula fixture\n" + MARKER + "\n"
                command = "python3 -c " + shlex.quote(
                    'from pathlib import Path; p=Path("memory_summary.md"); p.write_text('
                    + repr(summary)
                    + "); print(p.resolve())"
                )
                self.writes += 1
                call_id = "write_memory_" + str(self.writes)
            names = [t.get("name") for t in body.get("tools", [])]
            if "exec_command" in names:
                name, args = "exec_command", {"cmd": command, "max_output_tokens": 1000}
            elif "shell_command" in names:
                name, args = "shell_command", {"command": command}
            elif "shell" in names:
                name, args = "shell", {"command": ["/bin/bash", "-c", command]}
            else:
                raise RuntimeError("No supported consolidation tool: " + repr(names))
            return {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args),
            }
        return assistant("Fixture response.")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Codex:
    def __init__(self, binary, home, cwd, env):
        self.binary, self.home, self.cwd, self.env = binary, home, cwd, env
        self.pending = {}
        self.events = []
        self.serial = 0

    async def start(self):
        self.stderr_file = (self.home / "stderr.log").open("w")
        command = [self.binary, "app-server", "--listen", "stdio://"]
        if (self.home / ".omnigent-shared-memory.json").exists():
            from omnigent.harnesses.codex_native.memory import shared_memory_command

            command = shared_memory_command(self.home, command)
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.cwd,
            env={**self.env, "CODEX_HOME": str(self.home)},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr_file,
        )
        self.reader = asyncio.create_task(self.read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "memory_v2_experiment", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized"})
        return self

    async def read(self):
        while raw := await self.proc.stdout.readline():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.events.append(msg)
            if msg.get("id") in self.pending and ("result" in msg or "error" in msg):
                self.pending[msg["id"]].set_result(msg)
            elif "id" in msg and "method" in msg:
                await self.send(
                    {
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": "No human approval in acceptance harness",
                        },
                    }
                )

    async def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.serial += 1
        id = self.serial
        fut = asyncio.get_running_loop().create_future()
        self.pending[id] = fut
        await self.send({"id": id, "method": method, "params": params})
        response = await asyncio.wait_for(fut, 30)
        del self.pending[id]
        if "error" in response:
            raise RuntimeError((method, response["error"]))
        return response["result"]

    async def thread(self, config=None):
        params = {
            "cwd": str(self.cwd),
            "model": MODEL,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if config:
            params["config"] = config
        return (await self.request("thread/start", params))["thread"]["id"]

    async def turn(self, thread, prompt):
        result = await self.request(
            "turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]}
        )
        turn = result["turn"]["id"]
        for _ in range(300):
            for e in self.events:
                if (
                    e.get("method") == "turn/completed"
                    and e.get("params", {}).get("turn", {}).get("id") == turn
                ):
                    assert e["params"]["turn"]["status"] == "completed", e
                    return result
            await asyncio.sleep(0.1)
        raise TimeoutError("turn did not complete")

    async def close(self):
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except TimeoutError:
            self.proc.terminate()
            await self.proc.wait()
        await self.reader
        self.stderr_file.close()
        (self.home / "events.json").write_text(json.dumps(self.events, indent=2))


def config(url):
    return f'''model = "{MODEL}"
model_provider = "fixture"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
[model_providers.fixture]
name = "fixture"
base_url = "{url}"
wire_api = "responses"
requires_openai_auth = false
env_key = "MEMORY_TEST_KEY"
[features]
memories = true
shell_tool = true
code_mode = false
[memories]
version = "v2"
min_rollout_idle_hours = 1
extract_model = "{MODEL}"
consolidation_model = "{MODEL}"
'''


def provision(source, home):
    from omnigent.harnesses.codex_native.memory import prepare_shared_memory
    from omnigent.inner.codex_executor import _populate_codex_home_config

    home.mkdir(parents=True)
    _populate_codex_home_config(home, source, minimal_config=False)
    prepare_shared_memory(home, source)


async def run(root, binary, mode="shared-state", concurrent=False):
    def session_home(name):
        if mode == "native-control":
            return source
        home = root / name
        if mode in {"isolated", "symlink-control"}:
            home.mkdir()
            contents = config(mock.url)
            if mode == "symlink-control":
                contents = "sqlite_home = " + json.dumps(str(source)) + "\n" + contents
                (source / "memories_v2").mkdir(exist_ok=True)
                (home / "memories_v2").symlink_to(source / "memories_v2")
                (source / "memories_v2.lock").touch(exist_ok=True)
                (home / "memories_v2.lock").symlink_to(source / "memories_v2.lock")
            (home / "config.toml").write_text(contents)
        else:
            provision(source, home)
        return home

    root.mkdir(parents=True, exist_ok=True)
    mock = MockResponses(root)
    source = root / "source"
    source.mkdir()
    (source / "config.toml").write_text(config(mock.url))
    cwd = root / "workspace"
    cwd.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "home"),
        "MEMORY_TEST_KEY": "local-fixture-only",
        "RUST_LOG": "codex_memories_write=debug",
    }
    Path(env["HOME"]).mkdir()
    clients = []
    try:
        a_home = session_home("a")
        a = await Codex(binary, a_home, cwd, env).start()
        clients.append(a)
        a_id = await a.thread({"features.memories": False})
        await a.turn(a_id, MARKER)
        await a.close()
        clients.remove(a)
        db = (a_home if mode == "isolated" else source) / "state_5.sqlite"
        with sqlite3.connect(db) as con:
            stamp = int(time.time()) - 7 * 3600
            con.execute(
                "update threads set updated_at=?,updated_at_ms=? where id=?",
                (stamp, stamp * 1000, a_id),
            )
        print("A completed; scratch idle timestamp adjusted", flush=True)
        b_home = session_home("b")
        b = await Codex(binary, b_home, cwd, env).start()
        clients.append(b)
        b_id = await b.thread()
        if concurrent:
            b2_home = session_home("b2")
            b2 = await Codex(binary, b2_home, cwd, env).start()
            clients.append(b2)
            b2_id = await b2.thread()
            await asyncio.gather(
                b.turn(b_id, "Start an unrelated fixture task."),
                b2.turn(b2_id, "Start a concurrent fixture task."),
            )
        else:
            await b.turn(b_id, "Start an unrelated fixture task.")
        summary = (b_home if mode == "isolated" else source) / "memories_v2" / "memory_summary.md"
        for _ in range(600):
            if summary.exists() and (mode == "isolated" or MARKER in summary.read_text()):
                break
            if mock.errors:
                raise RuntimeError(mock.errors)
            if "failed preparing memories root" in (b_home / "stderr.log").read_text():
                if mode != "symlink-control":
                    raise RuntimeError((b_home / "stderr.log").read_text())
                assert mock.extractions == 0
                result = {
                    "mode": mode,
                    "expected_failure_confirmed": True,
                    "extractions": 0,
                    "reason": "Codex 0.165.0 rejects symbolic-link memory roots",
                    "root": str(root),
                }
                (root / "result.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2))
                return result
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("memory summary was not generated")
        if mode == "isolated":
            assert mock.extractions == 0 and MARKER not in summary.read_text()
            result = {
                "mode": mode,
                "extractions": 0,
                "cross_session_recall": False,
                "expected_failure_confirmed": True,
                "root": str(root),
            }
            (root / "result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            return result
        assert mock.extractions == 1 and mock.writes == 1, (mock.extractions, mock.writes)
        # Wait for the consolidation job to finish and validate its artifacts.
        for _ in range(100):
            with sqlite3.connect(source / "memories_v2_1.sqlite") as con:
                selected = con.execute(
                    "select selected_for_phase2 from stage1_outputs where thread_id=?", (a_id,)
                ).fetchone()
            if selected and selected[0]:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("consolidation did not mark source consumed")
        print("B extracted A and consolidation wrote and validated memory", flush=True)
        c_home = session_home("c")
        c = await Codex(binary, c_home, cwd, env).start()
        clients.append(c)
        c_id = await c.thread()
        before = len(mock.requests)
        await c.turn(c_id, "What do you recall about the fixture development server?")
        recall_prompt = "What do you recall about the fixture development server?"
        foreground = [
            r
            for r in mock.requests[before:]
            if any(
                item.get("role") == "user"
                and any(part.get("text") == recall_prompt for part in item.get("content", []))
                for item in r.get("input", [])
                if item.get("type") == "message"
            )
        ]
        assert len(foreground) == 1, "Expected exactly one C foreground request"
        assert MARKER in json.dumps(foreground[0]["input"]), (
            "C foreground did not receive learned fact"
        )
        cold_resume = False
        if mode == "shared-state":
            await c.close()
            clients.remove(c)
            from omnigent.harnesses.codex_native.memory import prepare_shared_memory

            prepare_shared_memory(c_home, source)
            resumed = await Codex(binary, c_home, cwd, env).start()
            clients.append(resumed)
            resume = await resumed.request("thread/resume", {"threadId": c_id})
            assert resume["thread"]["id"] == c_id
            await resumed.turn(c_id, "Confirm this same fixture conversation resumes.")
            cold_resume = True
        result = {
            "mode": mode,
            "concurrent": concurrent,
            "cold_resume": cold_resume,
            "codex_version": subprocess.check_output([binary, "--version"], text=True).strip(),
            "source_thread": a_id,
            "extractions": mock.extractions,
            "consolidation_requests": mock.consolidations,
            "actual_write_tool_calls": mock.writes,
            "cross_session_recall": True,
            "root": str(root),
        }
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return result
    finally:
        for client in clients:
            await client.close()
        mock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--mode",
        choices=["shared-state", "native-control", "isolated", "symlink-control"],
        default="shared-state",
    )
    parser.add_argument("--concurrent", action="store_true")
    args = parser.parse_args()
    root = args.artifacts or Path(tempfile.mkdtemp(prefix="omnigent-memory-v2-"))
    print("Artifacts:", root, flush=True)
    asyncio.run(run(root, str(Path(args.codex_bin).resolve()), args.mode, args.concurrent))
