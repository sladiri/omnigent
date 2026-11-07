"""Agent instructions reach real Codex despite project config overrides."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient, CodexNativeAppServer
from tests.e2e._codex_loopback_resume import (
    LoopbackResponsesProvider,
    clean_codex_env,
    wait_for_turn_end,
)


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires the Codex CLI")
@pytest.mark.timeout(90)
async def test_agent_instructions_survive_project_override_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    bridge = tmp_path / "bridge"
    home = tmp_path / "home"
    for path in (source, workspace, bridge, home):
        path.mkdir()
    (source / "config.toml").write_text('developer_instructions = "GLOBAL_GUIDANCE"\n')
    (workspace / ".git").mkdir()
    project_config = workspace / ".codex/config.toml"
    project_config.parent.mkdir()
    project_config.write_text('developer_instructions = ""\n')
    (workspace / "AGENTS.md").write_text("PROJECT_GUIDANCE\n")
    monkeypatch.setenv("CODEX_HOME", str(source))
    provider = LoopbackResponsesProvider()
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    codex = shutil.which("codex")
    assert codex is not None
    server = CodexNativeAppServer(
        codex_path=codex,
        socket_path=bridge / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env=clean_codex_env(home),
        config_overrides=[
            'model_provider="loopback"',
            "model_providers.loopback="
            f'{{name="Loopback",base_url={json.dumps(provider.base_url)},'
            'wire_api="responses",requires_openai_auth=false,'
            "request_max_retries=0,stream_max_retries=0}",
            "analytics.enabled=false",
            "feedback.enabled=false",
            "features.plugins=false",
            'otel.metrics_exporter="none"',
            "check_for_update_on_startup=false",
        ],
        developer_instructions="AGENT_GUIDANCE: Call workflow_status first.",
        cwd=workspace,
        bridge_dir=bridge,
        trust_project=True,
        reconcile_process_registry=False,
    )
    thread_id = None
    try:
        for resumed, custom_agent in ((False, True), (True, True), (False, False)):
            if not custom_agent:
                server.developer_instructions = None
            await server.start()
            client = CodexAppServerClient(server.socket_path)
            try:
                await client.connect()
                response = await client.request(
                    "thread/resume" if resumed else "thread/start",
                    {
                        **({"threadId": thread_id} if resumed else {}),
                        "model": "mock-model",
                        "cwd": str(workspace),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
                thread_id = response["result"]["thread"]["id"]
                await client.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "Prepare the workflow start message."}],
                    },
                )
                event = await asyncio.wait_for(wait_for_turn_end(client), timeout=30)
                assert event["method"] == "turn/completed", event
                model_input = provider.requests[-1]["input"]
                developer = json.dumps([m for m in model_input if m.get("role") == "developer"])
                assert ("AGENT_GUIDANCE" in developer) is custom_agent
                assert ("GLOBAL_GUIDANCE" in developer) is custom_agent
                assert "PROJECT_GUIDANCE" in json.dumps(model_input)
                assert developer.count("AGENT_GUIDANCE") == int(custom_agent)
            finally:
                await client.close()
                await server.close()
    finally:
        await server.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
    assert project_config.read_text() == 'developer_instructions = ""\n'
