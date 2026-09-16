"""Section contracts shared by every scraping workflow."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import Reference

# Returned as section text when a page comes back with its content gone and
# only LinkedIn's own navigation and footer left.
#
# Read carefully: that condition is a *guess* that the page was throttled, not
# an observation of one. It arrived in d8b4c62 with no cited evidence, LinkedIn
# documents no such behaviour, and nobody here has reproduced it deliberately —
# doing so would mean provoking a real throttle on a real account. The log line
# hedges with "likely" for the same reason.
#
# The same empty shell could also be a layout change, a resource this account
# cannot see, or a load that gave up. A session LinkedIn ended is the one
# alternative already ruled out elsewhere: every navigation checks the URL
# against the auth-blocker patterns first, and a redirect to /login, /authwall
# or /checkpoint raises before extraction is reached. That check stays on URLs
# deliberately — body text would be a per-locale guess, and this project's
# rule is that classification never depends on text values.
RATE_LIMITED_SECTION_TEXT = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# A submission is in flight from the moment the send is dispatched until the
# whole path has produced a result, cleanup included, and an interruption in
# that window cannot be reported. FastMCP runs every tool inside
# `anyio.fail_after()`, so the deadline raises `CancelledError` past
# `except Exception` and discards any result returned from the cancelled
# scope. The caller gets a timeout that carries no `retry_safe`, and this line
# is then the only record that a message may already have left. Answering the
# caller instead needs the tool to know its own deadline, which is issue #889.
SEND_INTERRUPTED_WARNING = (
    "Message submission was interrupted while in flight. The send outcome is "
    "unknown; check the conversation before retrying, as a retry may deliver "
    "the message twice."
)


def rate_limited_section_error() -> dict[str, str]:
    """The ``section_errors`` entry for a section that came back empty.

    One shape for every caller, because the alternative is what this codebase
    did until now: most call sites dropped the sentinel and returned the
    section as simply absent. An agent reading an empty section with no error
    concludes there was nothing to find and calls again, which is the opposite
    of what a rate limit asks for. Being told is what lets a client back off.

    Note this reports the *heuristic's* verdict, with the caveats on
    ``RATE_LIMITED_SECTION_TEXT`` above, and does not make it more accurate.
    What it changes is that a wrong verdict is now visible and can be argued
    with, where a silently missing section could not be.
    """
    return {
        "error_type": "rate_limit",
        "error_message": RATE_LIMITED_SECTION_TEXT,
    }


def message_action_result(
    url: str,
    status: str,
    message: str,
    *,
    recipient_selected: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
) -> dict[str, Any]:
    """Build a structured response for the send_message tool.

    ``sent`` is true only when the narrowly defined message-list UI transition
    was observed after submission. It does not prove delivery or that the
    recipient read the message. A caller keying a retry on it alone can re-send
    a message that may already have arrived, which is what ``retry_safe`` exists
    to say: it is false from the moment a submission is attempted, and true only
    while nothing can have left the composer.
    """
    return {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": recipient_selected,
        "sent": sent,
        "retry_safe": retry_safe,
    }


MESSAGE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_=-]+$")


def normalize_message_text(message: str) -> str:
    """Return the message with platform line endings folded to ``\n``."""
    return message.replace("\r\n", "\n").replace("\r", "\n")


def invalid_message_reason(message: str) -> str | None:
    """Explain why a message may not reach the composer, or None if it may.

    Line breaks are allowed: the browser side types each one as a paragraph
    break, the way Shift+Enter does. CR and CRLF count as line breaks because
    the sender folds them to LF. Every other C0 control and DEL is still
    refused before a session is acquired so no control input can reach the
    contenteditable surface.
    """
    message = normalize_message_text(message)
    if not message.strip():
        return "Message must contain non-whitespace characters."
    if any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in message
    ):
        return "Message must not contain control characters other than line breaks."
    return None


def refuse_an_invalid_message(
    linkedin_username: str, message: str
) -> dict[str, Any] | None:
    """Return the shared browser-free refusal for an unsafe message."""
    reason = invalid_message_reason(message)
    if reason is None:
        return None
    return message_action_result(
        person_profile_url(normalize_person_identifier(linkedin_username), "/"),
        "invalid_message",
        reason,
    )


def message_thread_url(thread_id: str) -> str:
    return f"https://www.linkedin.com/messaging/thread/{thread_id}/"


def refuse_an_invalid_thread_reply(
    thread_id: str, message: str
) -> dict[str, Any] | None:
    """Return the browser-free refusal for an unsafe thread reply, if any."""
    if not MESSAGE_THREAD_ID_RE.fullmatch(thread_id or ""):
        return message_action_result(
            "https://www.linkedin.com/messaging/",
            "invalid_thread",
            "thread_id must be a LinkedIn messaging thread ID "
            "(letters, digits, '_', '-' and '=').",
        )
    reason = invalid_message_reason(message)
    if reason is None:
        return None
    return message_action_result(
        message_thread_url(thread_id), "invalid_message", reason
    )


@dataclass
class ExtractedSection:
    """Text and compact references extracted from a loaded LinkedIn section."""

    text: str
    references: list[Reference]
    error: dict[str, Any] | None = None


class FilterValidationError(ValueError):
    """Invalid ``search_people`` filter input (network token / URN shape).

    Subclassing ``ValueError`` keeps backward-compatible behaviour for
    direct extractor callers (``pytest.raises(ValueError)`` matches), while
    letting the MCP tool wrapper catch this case precisely and surface the
    actionable message past ``mask_error_details``.
    """


JOB_ID_RE = re.compile(r"^[0-9]{6,}$")


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def refuse_an_invalid_job_message(job_id: str, message: str) -> dict[str, Any] | None:
    """Return the browser-free refusal for an unsafe job-poster message, if any."""
    if not JOB_ID_RE.fullmatch(job_id or ""):
        return message_action_result(
            "https://www.linkedin.com/jobs/",
            "invalid_job",
            "job_id must be a numeric LinkedIn job ID.",
        )
    reason = invalid_message_reason(message)
    if reason is None:
        return None
    return message_action_result(job_url(job_id), "invalid_message", reason)
