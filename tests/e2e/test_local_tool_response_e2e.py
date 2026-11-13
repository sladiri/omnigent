"""Large Python tool responses must release the isolated runner's worker pool."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.tools.local import _MAX_RESPONSE_BYTES
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle

_TOOL = b'''from omnigent_client import tool
import os
import time
from pathlib import Path

@tool
def large_result(label: str, size: int) -> str:
    """Return a large result over the response pipe."""
    assert "_AP_RESPONSE_FD" in os.environ, "must exercise the fd response protocol"
    root = Path(os.environ["_AP_WORKSPACE"])
    if label.isdigit():
        (root / label).touch()
        while not (root / "release").exists():
            time.sleep(0.02)
    return label + ":" + "x" * size
'''


@pytest.mark.timeout(150)
def test_parallel_large_results_release_runner_pool(tmp_path: Path) -> None:
    bundle = bundle_files(
        {
            "config.yaml": b"""spec_version: 1
name: large-result
prompt: Test local tool responses.
executor:
  type: omnigent
  config:
    harness: openai-agents
  model: unused
""",
            "tools/python/large_result.py": _TOOL,
        }
    )
    with server_runner(
        tmp_path / "stack", server_cwd=Path(__file__).resolve().parents[2]
    ) as stack:
        stack.start_runner(env={"OMNIGENT_RUNNER_THREADPOOL_MAX_WORKERS": "8"})
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=20,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            response = post_session_bundle(client.post, "/v1/sessions", bundle)
            response.raise_for_status()
            sid = response.json()["session_id"]
            bind_session_runner(client.patch, "", sid, stack.runner_id)

            def call(label: str, size: int) -> str:
                response = client.post(
                    f"/v1/sessions/{sid}/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": label,
                        "method": "tools/call",
                        "params": {
                            "name": "large_result",
                            "arguments": {"label": label, "size": size},
                        },
                    },
                )
                response.raise_for_status()
                body = response.json()
                assert "error" not in body, body
                return "".join(block["text"] for block in body["result"]["content"])

            assert call("warmup", 1) == "warmup:x"
            with ThreadPoolExecutor(max_workers=8) as callers:
                calls = [callers.submit(call, str(i), 128 * 1024) for i in range(8)]
                try:
                    deadline = time.monotonic() + 15
                    while not all((stack.workspace / str(i)).exists() for i in range(8)):
                        assert time.monotonic() < deadline, stack.log_tail()
                        time.sleep(0.05)
                finally:
                    (stack.workspace / "release").touch()
                for i, pending in enumerate(calls):
                    assert pending.result(timeout=20) == f"{i}:" + "x" * (128 * 1024)
            assert call("snapshot", 3 * 1024 * 1024) == "snapshot:" + "x" * (3 * 1024 * 1024)
            assert call("oversize", _MAX_RESPONSE_BYTES + 1) == (
                f"Error: tool response exceeds {_MAX_RESPONSE_BYTES}-byte limit"
            )
            assert call("after", 1) == "after:x"
            stack.restart_server()
            assert call("reconnected", 128 * 1024) == "reconnected:" + "x" * (128 * 1024)
