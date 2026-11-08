"""Easy Apply: fill a job's LinkedIn application with a message and a CV, then submit it.

The flow is LinkedIn's own modal, one step at a time: the CV goes into the
step's file input, the message into its first empty text area, and every other
field is what LinkedIn prefilled from the profile. A step with a required field
still empty stops the walk before anything is submitted (``needs_answers``,
the field labels returned), so no answer is ever guessed.

Locale independence (AGENTS.md Scraping Rules): every decision reads attribute
presence, never a label's words. The apply button, and the modal's next, review
and submit buttons, are found by the ``data-*`` hooks LinkedIn sets on them; the
labels are only returned, for the caller to show. A layout without those hooks
fails closed (``easy_apply_unavailable`` or ``stuck``), it never clicks a guess.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import linkedin_mcp_server.scraping.contracts as contracts
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

APPLY_BUTTON = "button[data-live-test-job-apply-button], button#jobs-apply-button-id"
_DIALOG = '[role="dialog"]:has([data-easy-apply-next-button], [data-live-test-easy-apply-review-button], [data-live-test-easy-apply-submit-button])'
NEXT_BUTTON = f"{_DIALOG} button[data-easy-apply-next-button]"
REVIEW_BUTTON = f"{_DIALOG} button[data-live-test-easy-apply-review-button]"
SUBMIT_BUTTON = f"{_DIALOG} button[data-live-test-easy-apply-submit-button]"
FILE_INPUT = f'{_DIALOG} input[type="file"]'

#: a modal longer than this is not an Easy Apply form we know
MAX_STEPS = 8
_OPEN_TIMEOUT_MS = 8000
_SUBMIT_TIMEOUT_MS = 15000

#: One read of the open step: which buttons it has, its empty required fields
#: and the ones LinkedIn marked invalid, by label (labels are returned, never
#: matched).
STEP_JS = """() => {
  const DIALOG = __DIALOG__;
  const d = document.querySelector(DIALOG);
  if (!d) return {open: false};
  const label = el => {
    const l = (el.labels && el.labels[0] && el.labels[0].innerText) ||
      (el.closest('fieldset') && el.closest('fieldset').querySelector('legend') &&
       el.closest('fieldset').querySelector('legend').innerText) ||
      el.getAttribute('aria-label') || el.name || el.id || '';
    return l.trim().replace(/\\s+/g, ' ').slice(0, 200);
  };
  const required = el => el.required || el.getAttribute('aria-required') === 'true';
  const empty = [];
  for (const el of d.querySelectorAll('input, select, textarea')) {
    if (el.disabled || el.type === 'hidden' || el.type === 'file' || !required(el)) continue;
    if (el.type === 'radio') {
      const group = d.querySelectorAll('input[type="radio"]');
      const checked = [...group].some(r => r.name === el.name && r.checked);
      if (!checked) empty.push(label(el));
    } else if (el.type === 'checkbox') {
      if (!el.checked) empty.push(label(el));
    } else if (!String(el.value || '').trim()) {
      empty.push(label(el));
    }
  }
  const textareas = [...d.querySelectorAll('textarea')].filter(t => !t.disabled && !t.value.trim()).length;
  return {
    open: true,
    submit: !!d.querySelector('[data-live-test-easy-apply-submit-button]'),
    review: !!d.querySelector('[data-live-test-easy-apply-review-button]'),
    next: !!d.querySelector('[data-easy-apply-next-button]'),
    file: !!d.querySelector('input[type="file"]'),
    textareas,
    empty: [...new Set(empty)],
    invalid: [...new Set([...d.querySelectorAll('[aria-invalid="true"]')].map(label))],
  };
}""".replace("__DIALOG__", json.dumps(_DIALOG))

#: what the screen says once the form is gone: the dialog LinkedIn shows after
#: a submission, else the page's main text
CONFIRMATION_JS = """() => {
  const d = [...document.querySelectorAll('[role="dialog"]')].pop();
  const el = d || document.querySelector('main') || document.body;
  return (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 500);
}"""


def easy_apply_result(
    url: str, status: str, message: str, **extra: Any
) -> dict[str, Any]:
    """The message_action_result shape, with what the walk saw."""
    sent = bool(extra.pop("sent", False))
    result = contracts.message_action_result(
        url, status, message, sent=sent, retry_safe=extra.pop("retry_safe", True)
    )
    result.update(extra)
    return result


class EasyApply:
    """Walk a job's Easy Apply modal and submit it on confirmation."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._page = session.page

    async def _shot(self, path: str | None) -> str | None:
        if not path:
            return None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            logger.debug("Could not take the Easy Apply screenshot", exc_info=True)
            return None

    async def easy_apply(
        self,
        job_id: str,
        message: str,
        *,
        cv_path: str | None = None,
        confirm_submit: bool,
        screenshot_path: str | None = None,
    ) -> dict[str, Any]:
        """Fill the job's Easy Apply form and submit it when ``confirm_submit``.

        ``sent`` is true only once the submit button was clicked and the form
        left the screen; ``retry_safe`` turns false at the click, as for a send.
        """
        message = contracts.normalize_message_text(message)
        refusal = contracts.refuse_an_invalid_job_message(job_id, message)
        if refusal is not None:
            return refusal
        url = contracts.job_url(job_id)
        if cv_path and not Path(cv_path).is_file():
            return easy_apply_result(url, "invalid_cv", f"No CV file at {cv_path}.")

        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._page

        button = page.locator(APPLY_BUTTON).first
        if await button.count() == 0:
            return easy_apply_result(
                page.url,
                "easy_apply_unavailable",
                "The job page shows no Easy Apply button (applied already, closed, or an external apply).",
            )
        await button.click()
        try:
            await page.wait_for_selector(_DIALOG, timeout=_OPEN_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return easy_apply_result(
                page.url, "easy_apply_unavailable", "The Easy Apply form did not open."
            )

        uploaded = typed = False
        for _ in range(MAX_STEPS):
            step = await page.evaluate(STEP_JS)
            if not step.get("open"):
                return easy_apply_result(
                    page.url, "dialog_closed", "The Easy Apply form closed."
                )
            if cv_path and step["file"] and not uploaded:
                await page.locator(FILE_INPUT).first.set_input_files(cv_path)
                uploaded = True
            if message and step["textareas"] and not typed:
                area = page.locator(f"{_DIALOG} textarea")
                for i in range(await area.count()):
                    if not (await area.nth(i).input_value()).strip():
                        await area.nth(i).fill(message)
                        typed = True
                        break
                step = await page.evaluate(STEP_JS)
            seen = {"cv_uploaded": uploaded, "message_typed": typed}
            if step["empty"]:
                return easy_apply_result(
                    page.url,
                    "needs_answers",
                    "A required field has no answer; nothing was submitted.",
                    fields=step["empty"],
                    screenshot=await self._shot(screenshot_path),
                    **seen,
                )
            if step["submit"]:
                if not confirm_submit:
                    return easy_apply_result(
                        page.url,
                        "ready",
                        "Filled up to the submit step; not submitted (confirm_submit is false).",
                        screenshot=await self._shot(screenshot_path),
                        **seen,
                    )
                await page.locator(SUBMIT_BUTTON).first.click()
                try:
                    await page.wait_for_selector(
                        SUBMIT_BUTTON, state="detached", timeout=_SUBMIT_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    return easy_apply_result(
                        page.url,
                        "submit_unconfirmed",
                        "Submit was clicked but the form stayed on screen.",
                        retry_safe=False,
                        screenshot=await self._shot(screenshot_path),
                        **seen,
                    )
                await self._session.delay(2)
                return easy_apply_result(
                    page.url,
                    "sent",
                    "Application submitted.",
                    sent=True,
                    retry_safe=False,
                    confirmation=await page.evaluate(CONFIRMATION_JS),
                    screenshot=await self._shot(screenshot_path),
                    **seen,
                )
            forward = (
                REVIEW_BUTTON
                if step["review"]
                else NEXT_BUTTON
                if step["next"]
                else None
            )
            if forward is None:
                return easy_apply_result(
                    page.url,
                    "stuck",
                    "The step has no next, review or submit button.",
                    screenshot=await self._shot(screenshot_path),
                    **seen,
                )
            await page.locator(forward).first.click()
            await self._session.delay(1)
            after = await page.evaluate(STEP_JS)
            if after.get("open") and after["invalid"]:
                return easy_apply_result(
                    page.url,
                    "needs_answers",
                    "LinkedIn refused a field on this step; nothing was submitted.",
                    fields=after["invalid"],
                    screenshot=await self._shot(screenshot_path),
                    **seen,
                )
        return easy_apply_result(
            page.url,
            "too_many_steps",
            f"The form had more than {MAX_STEPS} steps; nothing was submitted.",
            screenshot=await self._shot(screenshot_path),
        )
