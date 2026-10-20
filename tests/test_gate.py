"""The LinkedIn gate: the one load function, and what each answer does.

None of these reaches LinkedIn or a gate: each test answers for the gate, or
points the client at a port nothing listens on.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core import gate
from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)

pytestmark = pytest.mark.real_gate

PACKAGE = Path(gate.__file__).resolve().parents[1]
NAVIGATIONS = {"goto", "reload", "go_back", "go_forward"}


class _Page:
    def __init__(self, status: int = 200):
        self.visits: list[str] = []
        self.status = status

    async def goto(self, url: str, **_: Any) -> Any:
        self.visits.append(url)
        return type("Response", (), {"status": self.status})()


def _gate(monkeypatch, *answers: dict[str, Any] | None) -> list[tuple[str, dict]]:
    """Answer the gate's questions in turn; return what was asked."""
    asked: list[tuple[str, dict]] = []
    queue = list(answers)

    def answer(op, body, timeout=5.0):
        asked.append((op, body))
        return queue.pop(0) if queue else {"ok": True}

    monkeypatch.setattr(gate, "_ask", answer)
    return asked


def test_only_the_gate_navigates():
    """A new code path cannot load a LinkedIn page without asking the gate."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path == Path(gate.__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in NAVIGATIONS
            ):
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert offenders == []


async def test_a_refused_load_never_reaches_the_browser(monkeypatch):
    _gate(
        monkeypatch,
        {"allow": False, "reason": "LinkedIn gate: 3 write page loads an hour"},
    )
    page = _Page()
    with pytest.raises(gate.GateRefusedError, match="3 write page loads"):
        await gate.load(page, "https://www.linkedin.com/feed/", source="revalidate")
    assert page.visits == []


async def test_the_app_down_refuses_every_load(monkeypatch):
    monkeypatch.setenv("LINKEDIN_GATE_URL", "http://127.0.0.1:9/dashboard/linkedin/")
    page = _Page()
    for source in ("revalidate", "login", "import", "bridge", "tool:get_inbox", None):
        with pytest.raises(gate.GateRefusedError, match="fails closed"):
            await gate.load(page, "https://www.linkedin.com/feed/", source=source)
    assert page.visits == []


async def test_the_gap_is_waited_then_the_page_loads(monkeypatch):
    asked = _gate(monkeypatch, {"allow": False, "wait": 7}, {"allow": True})
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    monkeypatch.setattr(gate.asyncio, "sleep", sleep)
    page = _Page()
    token = gate.current_tool.set("search_jobs")
    try:
        await gate.load(page, "https://www.linkedin.com/jobs/search/")
    finally:
        gate.current_tool.reset(token)
    assert slept == [7]
    assert page.visits == ["https://www.linkedin.com/jobs/search/"]
    assert [b["source"] for _, b in asked] == ["tool:search_jobs"] * 2


async def test_a_gap_too_long_is_a_refusal(monkeypatch):
    _gate(monkeypatch, {"allow": False, "wait": gate.MAX_WAIT_SECONDS + 1})
    with pytest.raises(gate.GateRefusedError):
        await gate.load(_Page(), "https://www.linkedin.com/feed/", source="login")


async def test_a_skip_spares_only_the_session_recheck(monkeypatch):
    _gate(monkeypatch, {"allow": False, "skip": True}, {"allow": False, "skip": True})
    page = _Page()
    assert (
        await gate.load(page, "https://www.linkedin.com/feed/", source="revalidate")
        is gate.SKIPPED
    )
    with pytest.raises(gate.GateRefusedError):
        await gate.load(page, "https://www.linkedin.com/feed/", source="import")
    assert page.visits == []


async def test_a_limit_status_starts_the_cooldown(monkeypatch):
    asked = _gate(monkeypatch, {"allow": True}, {"ok": True})
    with pytest.raises(RateLimitError):
        await gate.load(
            _Page(status=429),
            "https://www.linkedin.com/in/me/",
            source="tool:get_my_profile",
        )
    assert asked[1][0] == "limited" and "HTTP 429" in asked[1][1]["why"]


async def test_a_limit_answer_from_a_tool_starts_the_cooldown(monkeypatch):
    asked = _gate(monkeypatch, {"ok": True})
    with pytest.raises(ToolError, match="Rate limit"):
        raise_tool_error(RateLimitError("Rate limit detected"), "get_my_profile")
    await asyncio.gather(*gate._reports)
    assert [op for op, _ in asked] == ["limited"]


async def test_a_limit_the_load_reported_is_not_reported_again(monkeypatch):
    asked = _gate(monkeypatch, {"allow": True}, {"ok": True})
    with pytest.raises(RateLimitError) as raised:
        await gate.load(
            _Page(status=999), "https://www.linkedin.com/feed/", source="revalidate"
        )
    with pytest.raises(ToolError):
        raise_tool_error(raised.value, "get_inbox")
    await asyncio.gather(*gate._reports)
    assert [op for op, _ in asked] == ["load", "limited"]


async def test_a_cut_answer_is_no_answer(monkeypatch):
    import http.client

    def cut(*_a, **_k):
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(gate.urllib.request, "urlopen", cut)
    with pytest.raises(gate.GateRefusedError, match="fails closed"):
        await gate.load(_Page(), "https://www.linkedin.com/feed/", source="login")


async def test_a_send_forgets_the_cached_inbox(monkeypatch):
    asked = _gate(monkeypatch)
    mcp = FastMCP("test")
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    @mcp.tool
    async def reply_in_thread() -> dict[str, Any]:
        return {"sent": True}

    await mcp.call_tool("reply_in_thread", {})
    assert asked == [("forget", {"kind": "inbox"})]


def test_a_gate_refusal_is_not_reported_as_a_limit(monkeypatch):
    asked = _gate(monkeypatch)
    with pytest.raises(ToolError, match="Do not retry"):
        raise_tool_error(
            gate.GateRefusedError("LinkedIn cooldown until ..."), "get_inbox"
        )
    assert asked == []


async def test_a_refused_session_check_is_not_a_dead_session(monkeypatch):
    """A refusal must not read as a dead session: that retires the profile."""
    from linkedin_mcp_server.drivers.browser import _feed_auth_succeeds

    _gate(monkeypatch, {"allow": False, "reason": "LinkedIn cooldown until tomorrow"})
    browser: Any = type("Browser", (), {"page": _Page()})()
    with pytest.raises(gate.GateRefusedError):
        await _feed_auth_succeeds(browser)
    assert browser.page.visits == []


async def test_a_skipped_session_check_counts_as_checked(monkeypatch):
    from linkedin_mcp_server.drivers.browser import _feed_auth_succeeds

    _gate(monkeypatch, {"allow": False, "skip": True})
    browser: Any = type("Browser", (), {"page": _Page()})()
    assert await _feed_auth_succeeds(browser) is True
    assert browser.page.visits == []


async def test_a_cached_read_loads_no_page(monkeypatch):
    kept = {"url": "https://www.linkedin.com/messaging/", "sections": {"inbox": "x"}}
    asked = _gate(monkeypatch, {"body": json.dumps(kept)})
    runs: list[int] = []
    mcp = FastMCP("test")
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    @mcp.tool
    async def get_inbox(limit: int) -> dict[str, Any]:
        runs.append(limit)
        return {"fresh": True}

    result = await mcp.call_tool("get_inbox", {"limit": 20})
    assert result.structured_content == kept
    assert runs == []
    assert asked == [("cached", {"key": 'get_inbox|{"limit": 20}'})]


async def test_a_fresh_read_is_kept_with_its_kind(monkeypatch):
    asked = _gate(monkeypatch, {"body": None}, {"ok": True})
    mcp = FastMCP("test")
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    @mcp.tool
    async def get_invitations() -> dict[str, Any]:
        return {"sections": {"invitations": "y"}}

    await mcp.call_tool("get_invitations", {})
    op, body = asked[1]
    assert op == "cache" and body["kind"] == "invitations"
    assert json.loads(body["body"]) == {"sections": {"invitations": "y"}}


async def test_a_write_is_never_cached(monkeypatch):
    asked = _gate(monkeypatch)
    mcp = FastMCP("test")
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    @mcp.tool
    async def send_message() -> dict[str, Any]:
        return {"sent": True}

    await mcp.call_tool("send_message", {})
    assert [op for op, _ in asked] == ["forget"]
