"""Scratch acceptance test: real Codex runtime, deterministic local inference only."""

from __future__ import annotations

import asyncio
import gzip
import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                    'print("\\n".join(p.read_text() for p in '
                    'Path("rollout_summaries").glob("*.md")))'
                )
                call_id = "read_memory_" + str(self.consolidations)
            else:
                supported = MARKER in json.dumps(reads)
                summary = (
                    "v1\n## User Profile\n\n## User preferences\n\n"
                    "## General Tips\n\n## What's in Memory\n"
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
        self.proc = await asyncio.create_subprocess_exec(
            self.binary,
            "app-server",
            "--listen",
            "stdio://",
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
