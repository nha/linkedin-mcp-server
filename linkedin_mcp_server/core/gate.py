"""The one way to load a LinkedIn page: ask the gate, then navigate.

LinkedIn meters page loads, and an account it bans is gone for good. Counting tool
calls missed most of them: every browser start re-checked the session on
``/feed/``, a login opened ``/login``, a cookie import loaded the feed twice, and
none of these is a tool call. So the count is taken where the page is loaded, in
:func:`load`, and nowhere else: ``tests/test_gate.py`` fails on any other
``goto``, ``reload``, ``go_back`` or ``go_forward`` in the package.

The gate is an HTTP service outside this process (``LINKEDIN_GATE_URL``; the
dashboard that holds the caps, the cooldown and the cache in its store). It
answers ``allow``, ``wait`` (the minimum gap: :func:`load` sleeps and asks
again), ``skip`` (a session re-check a recent load made useless) or a refusal
with its ``reason``. No answer is a refusal: a gate that is down never lets a
load through.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import urllib.request
from typing import Any, NoReturn

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.core.proxy_errors import raise_if_proxy_error

logger = logging.getLogger(__name__)

#: The longest the gap may hold one load before it is a refusal instead: the
#: write gap, so a send's waits stay well inside the tool's 180 s deadline and
#: never push its submit past it (/code-review of #8).
MAX_WAIT_SECONDS = 60

#: The tool call this load belongs to, set by the tool middleware.
current_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "linkedin_gate_tool", default=None
)

#: What :func:`load` returns for a skipped session re-check (``page.goto`` may
#: itself return None, so None cannot say it).
SKIPPED = object()

#: HTTP statuses LinkedIn answers a client it is limiting with (999 is its own).
_LIMIT_STATUSES = (429, 999)


class GateRefusedError(RateLimitError):
    """The gate refused the load, or did not answer. The session is kept.

    A rate limit for every caller: what it refuses is not evidence about the
    session, so nothing retires the profile or opens a login over it.
    """

    def __init__(self, message: str):
        super().__init__(message, suggested_wait_time=0)


def _gate_url() -> str:
    return (
        os.environ.get(
            "LINKEDIN_GATE_URL", "http://127.0.0.1:7788/dashboard/linkedin/"
        ).rstrip("/")
        + "/"
    )


def _ask(op: str, body: dict[str, Any], timeout: float = 5.0) -> dict[str, Any] | None:
    """The gate's JSON answer to *op*, or None when it does not answer."""
    request = urllib.request.Request(
        _gate_url() + op,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json", "x-dashboard-cli": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)
    except Exception:
        # An HTTP status, a body cut short (http.client.HTTPException is not an
        # OSError), bad JSON: all of it is no answer, and no answer refuses.
        return None
    return answer if isinstance(answer, dict) else None


async def ask(op: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """:func:`_ask` off the event loop."""
    return await asyncio.to_thread(_ask, op, body)


def source_now() -> str:
    """Who is loading: the running tool, or a run outside any tool call."""
    tool = current_tool.get()
    return f"tool:{tool}" if tool else "direct"


async def load(
    page: Any, url: str, *, source: str | None = None, **goto_kwargs: Any
) -> Any:
    """Navigate *page* to *url* once the gate allows it; the only ``page.goto``.

    Returns the navigation's response, or :data:`SKIPPED` when the gate answered ``skip``
    (only a ``revalidate`` gets that answer, and its caller treats the session
    as checked). Raises :class:`GateRefusedError` on a refusal or no answer,
    and :class:`RateLimitError` after reporting it when LinkedIn answers with a
    limit status.
    """
    source = source or source_now()
    waited = 0.0
    while True:
        answer = await ask("load", {"source": source, "url": url})
        if answer is None:
            raise GateRefusedError(
                "LinkedIn gate: no answer, so no LinkedIn page loads (it fails "
                "closed). Start the dashboard; do not work around it."
            )
        if answer.get("allow") is True:
            break
        if answer.get("skip") is True and source == "revalidate":
            logger.info("Gate: session re-check skipped, a recent load passed")
            return SKIPPED
        wait = answer.get("wait")
        if (
            isinstance(wait, int | float)
            and 0 < wait
            and waited + wait <= MAX_WAIT_SECONDS
        ):
            await asyncio.sleep(wait)
            waited += wait
            continue
        raise GateRefusedError(
            str(answer.get("reason") or f"LinkedIn gate refused {url}: {answer}")
        )
    try:
        response = await page.goto(url, **goto_kwargs)
    except Exception as exc:
        raise_if_proxy_error(exc)
        # Chromium raises this for an error status with an empty body, which is
        # how the 429s of 2026-10-18 came: taken as a limit, wherever it loads.
        if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc):
            await _limited(f"{source} got an empty error answer on {url}")
        raise
    status = getattr(response, "status", None)
    if status in _LIMIT_STATUSES:
        await _limited(f"{source} got HTTP {status} on {url}")
    return response


async def _limited(why: str) -> NoReturn:
    """Report the limit, then raise it marked as reported."""
    await report_limited(why)
    limited = RateLimitError(f"LinkedIn answered {why}", suggested_wait_time=86400)
    setattr(limited, "reported", True)  # report_limited_soon skips it
    raise limited


async def report_limited(why: str) -> None:
    """LinkedIn answered with a limit: the gate stops every load for a day."""
    if not (await ask("limited", {"why": why}) or {}).get("ok"):
        logger.warning("Gate did not take the limit report (%s)", why)


_reports: set[asyncio.Task[None]] = set()


def report_limited_soon(exc: BaseException, why: str) -> None:
    """:func:`report_limited` from synchronous code (the tool error handler),
    once per limit: :func:`load` has already reported the ones it raised. On
    the running loop as a task, so the report never blocks it."""
    if getattr(exc, "reported", False):
        return
    try:
        task = asyncio.get_running_loop().create_task(report_limited(why))
    except RuntimeError:
        if not (_ask("limited", {"why": why}, timeout=2.0) or {}).get("ok"):
            logger.warning("Gate did not take the limit report (%s)", why)
        return
    _reports.add(task)
    task.add_done_callback(_reports.discard)


async def hello(version: str) -> None:
    """Tell the gate this server asks it: until one has, the client hook refuses every call."""
    if not (await ask("hello", {"version": version}) or {}).get("ok"):
        logger.warning(
            "Gate did not answer hello; the client hook refuses calls until one does"
        )


#: Reads the gate keeps, and the time-to-live kind each one is kept under.
CACHED_KINDS = {
    "get_person_profile": "profile",
    "get_my_profile": "profile",
    "get_company_profile": "company",
    "get_company_employees": "company",
    "get_company_posts": "company",
    "get_inbox": "inbox",
    "get_conversation": "inbox",
    "search_conversations": "inbox",
    "search_jobs": "jobs",
    "get_job_details": "jobs",
    "get_saved_jobs": "jobs",
    "get_invitations": "invitations",
}


def cache_key(tool: str, arguments: dict[str, Any] | None) -> str:
    return tool + "|" + json.dumps(arguments or {}, sort_keys=True, default=str)


async def cached(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """The structured result the gate kept for this call and still holds good, or None."""
    if tool not in CACHED_KINDS:
        return None
    body = (await ask("cached", {"key": cache_key(tool, arguments)}) or {}).get("body")
    try:
        value = json.loads(body) if isinstance(body, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


#: What a write makes stale: a send changes the inbox, an invitation the invitations.
FORGETS = {
    "send_message": "inbox",
    "reply_in_thread": "inbox",
    "message_job_poster": "inbox",
    "easy_apply": "jobs",
    "connect_with_person": "invitations",
}


async def forget(tool: str) -> None:
    """Drop the cached reads a write makes wrong, so a check during or right
    after a send never reads the thread from before it (and sends again)."""
    if kind := FORGETS.get(tool):
        await ask("forget", {"kind": kind})


async def keep(
    tool: str, arguments: dict[str, Any] | None, result: dict[str, Any]
) -> None:
    """Keep a complete read: one with a section error is not kept."""
    if tool not in CACHED_KINDS or result.get("section_errors"):
        return
    await ask(
        "cache",
        {
            "key": cache_key(tool, arguments),
            "kind": CACHED_KINDS[tool],
            "body": json.dumps(result, default=str),
        },
    )
