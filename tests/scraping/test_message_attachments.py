"""The attachment list is refused before a browser is touched."""

from __future__ import annotations

from pathlib import Path

from linkedin_mcp_server.scraping.message_sender import (
    _ATTACHMENT_MAX_FILES,
    _invalid_attachments_reason,
)


def _pdf(tmp_path: Path, name: str, size: int = 16) -> str:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4" + b"0" * size)
    return str(path)


def test_no_attachments_is_allowed(tmp_path: Path) -> None:
    assert _invalid_attachments_reason([]) is None


def test_a_real_pdf_is_allowed(tmp_path: Path) -> None:
    assert _invalid_attachments_reason([_pdf(tmp_path, "cv.pdf")]) is None


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    reason = _invalid_attachments_reason(["private/cv/cv.pdf"])
    assert reason is not None and "absolute" in reason


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    reason = _invalid_attachments_reason([str(tmp_path / "nope.pdf")])
    assert reason is not None and "no such file" in reason


def test_an_unexpected_extension_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "payload.sh"
    path.write_text("#!/bin/sh\n")
    reason = _invalid_attachments_reason([str(path)])
    assert reason is not None and ".sh" in reason


def test_too_many_files_are_refused(tmp_path: Path) -> None:
    paths = [_pdf(tmp_path, f"cv{i}.pdf") for i in range(_ATTACHMENT_MAX_FILES + 1)]
    reason = _invalid_attachments_reason(paths)
    assert reason is not None and "at most" in reason


def test_oversized_files_are_refused(tmp_path: Path) -> None:
    reason = _invalid_attachments_reason([_pdf(tmp_path, "big.pdf", 11 * 1024 * 1024)])
    assert reason is not None and "limit" in reason
