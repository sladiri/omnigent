"""Prepared workflow approval from the built Web UI, with a mocked backend."""

import json

import pytest
from playwright.sync_api import expect

from tests.browser_ui.chat._geometry_helpers import open_live


@pytest.mark.parametrize("width", [390, 1440])
def test_start_workflow_submits_current_draft_without_json(page, chat_session_contract, width):
    chat = chat_session_contract
    chat.agent_id = "workflow-controller"
    draft = {
        "token": "validated-first",
        "workflow": "implement-review-fix",
        "repository": "/repo",
        "task": "Filter certificates",
        "baseline": "main",
        "base": "main",
        "branch": "feature/lcs-1670",
        "publish": False,
    }
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/workflow-draft", lambda _: {"draft": draft}
    )
    open_live(page, chat, {"width": width, "height": 1000})
    start = page.get_by_role("button", name="Start workflow", exact=True)
    expect(start).to_be_enabled()
    assert not chat.event_posts
    page.get_by_text("Review prepared workflow", exact=True).click()
    expect(
        page.get_by_text("Branch: feature/lcs-1670 · Publication: disabled", exact=True)
    ).to_be_visible()
    page.get_by_label("Message the agent").fill("Change the task")
    expect(start).to_be_disabled()
    page.get_by_label("Message the agent").fill("")
    draft["token"] = "validated-second"
    page.reload()
    expect(start).to_be_enabled()
    page.get_by_text("Review prepared workflow", exact=True).click()
    start.scroll_into_view_if_needed()
    box = start.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    page.screenshot(path=f"/tmp/workflow-start-{width}.png")
    start.click()
    page.wait_for_timeout(300)
    assert len(chat.event_posts) == 1
    assert "/workflow start-draft validated-second" in json.dumps(chat.event_posts[0]["body"])
    assert "/workflow start {" not in json.dumps(chat.event_posts[0]["body"])
