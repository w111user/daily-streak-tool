from __future__ import annotations

import asyncio
import unittest

from daily_streak.tiktok import CAPTCHA_PROBE_TIMEOUT_MS, is_captcha_present


class CaptchaProbeTests(unittest.TestCase):
    def test_captcha_probes_use_short_explicit_timeouts(self) -> None:
        class Locator:
            first = None

            def __init__(self, selector: str, calls: list[tuple[str, int]]) -> None:
                self.selector = selector
                self.calls = calls
                self.first = self

            async def is_visible(self, *, timeout: int) -> bool:
                self.calls.append((self.selector, timeout))
                return False

            async def inner_text(self, *, timeout: int) -> str:
                self.calls.append((self.selector, timeout))
                return "normal TikTok page"

        class Page:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            def locator(self, selector: str) -> Locator:
                return Locator(selector, self.calls)

        page = Page()
        self.assertFalse(asyncio.run(is_captcha_present(page)))

        self.assertTrue(page.calls)
        self.assertTrue(all(timeout == CAPTCHA_PROBE_TIMEOUT_MS for _, timeout in page.calls))
        self.assertGreaterEqual(CAPTCHA_PROBE_TIMEOUT_MS, 500)
        self.assertLessEqual(CAPTCHA_PROBE_TIMEOUT_MS, 1000)
