"""Tests for the section contracts every scraping workflow returns."""

from typing import Any

import pytest

from linkedin_mcp_server.scraping import contracts
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    SEND_INTERRUPTED_WARNING,
    ExtractedSection,
    FilterValidationError,
    message_action_result,
    normalize_message_text,
    rate_limited_section_error,
    refuse_an_invalid_message,
    refuse_an_invalid_thread_reply,
)


class TestRateLimitedSection:
    def test_the_sentinel_text_is_what_reaches_the_client(self):
        # Pinned as a literal on purpose. Every other assertion in the suite
        # compares a result against this same constant, so it moves with any
        # edit and none of them can see the message a client would read.
        assert RATE_LIMITED_SECTION_TEXT == (
            "[Rate limited] LinkedIn blocked this section. "
            "Try again later or request fewer sections."
        )

    def test_the_reported_error_repeats_the_sentinel_verbatim(self):
        # The tools compare a section's text against the sentinel and then
        # report this error, so the two drifting apart would describe a
        # section the caller never saw.
        assert rate_limited_section_error() == {
            "error_type": "rate_limit",
            "error_message": RATE_LIMITED_SECTION_TEXT,
        }


class TestExtractedSection:
    def test_a_section_without_an_error_carries_none(self):
        section = ExtractedSection(text="Bill Gates", references=[])

        assert section.error is None

    def test_an_error_is_kept_beside_the_text(self):
        section = ExtractedSection(
            text="", references=[], error=rate_limited_section_error()
        )

        assert section.text == ""
        assert section.error == rate_limited_section_error()


class TestFilterValidationError:
    def test_it_is_still_a_value_error(self):
        # Direct extractor callers catch ValueError; the tool wrappers catch
        # this subclass to surface the message past mask_error_details.
        assert issubclass(FilterValidationError, ValueError)


class TestMessageActionResult:
    def test_the_retry_contract_is_explicit_on_every_result(self):
        assert message_action_result(
            "https://www.linkedin.com/messaging/compose/",
            "sent",
            "Message submitted.",
            recipient_selected=True,
            sent=True,
            retry_safe=False,
        ) == {
            "url": "https://www.linkedin.com/messaging/compose/",
            "status": "sent",
            "message": "Message submitted.",
            "recipient_selected": True,
            "sent": True,
            "retry_safe": False,
        }

    def test_the_interruption_warning_names_duplicate_delivery(self):
        assert SEND_INTERRUPTED_WARNING == (
            "Message submission was interrupted while in flight. The send outcome "
            "is unknown; check the conversation before retrying, as a retry may "
            "deliver the message twice."
        )


class TestRefuseAnInvalidMessage:
    @pytest.mark.parametrize("message", ["before\tafter", "text\x7f", "nul\x00"])
    def test_every_c0_or_del_character_but_line_breaks_is_refused(self, message: str):
        assert refuse_an_invalid_message("alice", message) == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must not contain control characters other than line breaks.",
        )

    @pytest.mark.parametrize("message", ["line\nbreak", "line\r\nbreak", "line\rbreak"])
    def test_line_breaks_in_any_platform_form_are_accepted(self, message: str):
        assert refuse_an_invalid_message("alice", message) is None

    def test_line_endings_fold_to_lf(self):
        assert normalize_message_text("a\r\nb\rc\nd") == "a\nb\nc\nd"

    @pytest.mark.parametrize("thread_id", ["", "a/b", "a?b", "a b", "a#b"])
    def test_thread_reply_refuses_a_malformed_thread_id(self, thread_id: str):
        result = refuse_an_invalid_thread_reply(thread_id, "hello")
        assert result is not None
        assert result["status"] == "invalid_thread"
        assert result["retry_safe"] is True

    def test_thread_reply_refuses_controls_on_the_thread_url(self):
        assert refuse_an_invalid_thread_reply("2-abc==", "a\tb") == (
            message_action_result(
                "https://www.linkedin.com/messaging/thread/2-abc==/",
                "invalid_message",
                "Message must not contain control characters other than line breaks.",
            )
        )

    def test_thread_reply_accepts_a_multi_line_message(self):
        assert refuse_an_invalid_thread_reply("2-abc==", "a\n\nb") is None

    def test_whitespace_is_refused_before_normal_message_text(self):
        assert refuse_an_invalid_message("alice", "   ") == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must contain non-whitespace characters.",
        )

    def test_safe_single_line_text_is_accepted(self):
        assert refuse_an_invalid_message("alice", "Hello, Alice!") is None

    def test_the_refusal_calls_the_owner_constructor_directly(self, monkeypatch):
        calls: list[tuple[str, str, str]] = []
        sentinel: dict[str, Any] = {"owner": "contracts"}

        def constructor(url: str, status: str, message: str) -> dict[str, Any]:
            calls.append((url, status, message))
            return sentinel

        monkeypatch.setattr(contracts, "message_action_result", constructor)

        assert refuse_an_invalid_message("alice", "") is sentinel
        assert calls == [
            (
                "https://www.linkedin.com/in/alice/",
                "invalid_message",
                "Message must contain non-whitespace characters.",
            )
        ]
