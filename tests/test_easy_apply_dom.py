"""Browser-DOM tests for easy_apply against a stand-in Easy Apply modal.

The stand-in mirrors LinkedIn's modal: a ``data-live-test-job-apply-button``
on the job page opens a ``role="dialog"`` whose steps carry the
``data-easy-apply-next-button``, ``data-live-test-easy-apply-review-button``
and ``data-live-test-easy-apply-submit-button`` hooks. Every label is an opaque
token, so a pass can only come from attributes, never a word (AGENTS.md
Scraping Rules). Skipped when chromium is not installed, as the other DOM tests. What the page
saw is read back from ``body`` data attributes: patchright evaluates in an
isolated world, where the page's own globals are not visible.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.easy_apply import EasyApply
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

JOB_ID = "4469816503"
JOB_URL = f"https://www.linkedin.com/jobs/view/{JOB_ID}/"
MESSAGE = "Bonjour,\nligne deux"


def job_page(steps: list[str], *, button: bool = True) -> str:
    apply = (
        '<button data-live-test-job-apply-button id="b">qx1</button>' if button else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<main><h1>qx0</h1>{apply}</main>
<script>
const steps = {json.dumps(steps)};
let i = 0;
function show() {{
  document.querySelector('[role="dialog"]')?.remove();
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.innerHTML = '<form>' + steps[i] + '</form>';
  document.body.appendChild(d);
  d.querySelector('[data-easy-apply-next-button], [data-live-test-easy-apply-review-button]')
    ?.addEventListener('click', e => {{ e.preventDefault(); i += 1; show(); }});
  d.querySelector('[data-live-test-easy-apply-submit-button]')
    ?.addEventListener('click', e => {{
      e.preventDefault();
      d.remove();
      const ok = document.createElement('div');
      ok.setAttribute('role', 'dialog');
      ok.innerText = 'qx-sent';
      document.body.appendChild(ok);
    }});
  d.querySelector('input[type=file]')?.addEventListener('change', e => {{ document.body.dataset.file = e.target.files[0].name; }});
  d.querySelector('textarea')?.addEventListener('input', e => {{ document.body.dataset.text = e.target.value; }});
}}
document.getElementById('b')?.addEventListener('click', () => {{ show(); }});
</script></body></html>"""


CONTACT = (
    '<label for="e">qx2</label><input id="e" required value="nicolas@klar.im">'
    "<button data-easy-apply-next-button>qx3</button>"
)
RESUME = '<input type="file" id="f"><button data-easy-apply-next-button>qx4</button>'
MORE = (
    '<label for="t">qx5</label><textarea id="t"></textarea>'
    "<button data-live-test-easy-apply-review-button>qx6</button>"
)
REVIEW = "<button data-live-test-easy-apply-submit-button>qx7</button>"
QUESTION = (
    '<label for="q">qx8</label><input id="q" aria-required="true" value="">'
    "<button data-easy-apply-next-button>qx9</button>"
)


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(2000)
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<html></html>"
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


async def apply(page, html: str, tmp_path, *, confirm_submit: bool = True) -> dict:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 stand-in")

    async def navigate(self, url):
        await page.goto(JOB_URL)
        await page.set_content(html)

    async def no_delay(self, seconds):
        return None

    with (
        patch.object(PageNavigator, "_navigate_to_page", navigate),
        patch.object(ScrapingSession, "check_rate_limit", return_value=None),
        patch.object(ScrapingSession, "delay", no_delay),
    ):
        session = ScrapingSession(page)
        return await EasyApply(session, PageNavigator(session)).easy_apply(
            JOB_ID,
            MESSAGE,
            cv_path=str(cv),
            confirm_submit=confirm_submit,
            screenshot_path=str(tmp_path / "proof.png"),
        )


async def test_every_step_filled_then_submitted(dom_page, tmp_path):
    result = await apply(dom_page, job_page([CONTACT, RESUME, MORE, REVIEW]), tmp_path)
    assert result["status"] == "sent", result
    assert result["sent"] is True and result["retry_safe"] is False
    assert result["cv_uploaded"] and result["message_typed"]
    assert await dom_page.evaluate("document.body.dataset.file") == "cv.pdf"
    assert await dom_page.evaluate("document.body.dataset.text") == MESSAGE
    assert result["confirmation"] == "qx-sent"
    assert (tmp_path / "proof.png").stat().st_size > 0


async def test_without_confirmation_nothing_is_submitted(dom_page, tmp_path):
    result = await apply(
        dom_page,
        job_page([CONTACT, RESUME, MORE, REVIEW]),
        tmp_path,
        confirm_submit=False,
    )
    assert result["status"] == "ready" and result["sent"] is False
    assert (
        await dom_page.locator("[data-live-test-easy-apply-submit-button]").count() == 1
    )


async def test_an_unanswered_required_field_stops_before_submit(dom_page, tmp_path):
    result = await apply(dom_page, job_page([CONTACT, QUESTION, REVIEW]), tmp_path)
    assert result["status"] == "needs_answers" and result["sent"] is False
    assert result["fields"] == ["qx8"]
    assert result["retry_safe"] is True


async def test_no_easy_apply_button_fails_closed(dom_page, tmp_path):
    result = await apply(dom_page, job_page([REVIEW], button=False), tmp_path)
    assert result["status"] == "easy_apply_unavailable" and result["sent"] is False


async def test_a_missing_cv_is_refused_before_any_page(dom_page, tmp_path):
    session = ScrapingSession(dom_page)
    result = await EasyApply(session, PageNavigator(session)).easy_apply(
        JOB_ID, MESSAGE, cv_path=str(tmp_path / "none.pdf"), confirm_submit=True
    )
    assert result["status"] == "invalid_cv"
