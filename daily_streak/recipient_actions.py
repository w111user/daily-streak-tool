"""Browser-session adapters for shared recipient discovery operations."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence, TypeVar

from daily_streak.browser import (
    get_action_timeout_ms,
    get_content_ready_timeout_ms,
    get_dm_load_attempts,
    get_dm_readiness_timeout_ms,
    get_low_memory_mode,
    get_low_memory_stabilization_seconds,
    get_navigation_timeout_ms,
    get_probe_timeout_ms,
    launch_browser_session,
)


ResultType = TypeVar("ResultType")


class NoRecipientsSelectedError(ValueError):
    """Raised before browser startup when a resolve operation has no targets."""


async def _run_with_recipient_page(
    config: Mapping[str, Any],
    operation: Callable[[Any], Awaitable[ResultType]],
    *,
    playwright_factory: Callable[[], Any] | None = None,
) -> ResultType:
    """Run one recipient operation and always close its temporary context."""

    if playwright_factory is None:
        from playwright.async_api import async_playwright

        playwright_factory = async_playwright

    async with playwright_factory() as playwright:
        session = await launch_browser_session(playwright, config)
        try:
            await session.load_cookies(config.get("cookie_file", "cookies.json"))
            page = await session.get_page()
            return await operation(page)
        finally:
            await session.close()


async def scan_recipients(
    config: Mapping[str, Any],
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, str]]:
    """Scan TikTok DM conversations with one short-lived shared session.

    ``on_progress`` is accepted for adapter consistency; TikTok only exposes a
    final result for the current scan implementation.
    """

    del on_progress
    from daily_streak.recipients import RecipientService

    service = RecipientService(
        navigation_timeout_ms=get_navigation_timeout_ms(config),
        action_timeout_ms=get_action_timeout_ms(config),
        content_ready_timeout_ms=get_content_ready_timeout_ms(config),
        dm_readiness_timeout_ms=get_dm_readiness_timeout_ms(config),
        dm_load_attempts=get_dm_load_attempts(config),
        probe_timeout_ms=get_probe_timeout_ms(config),
        low_memory_mode=get_low_memory_mode(config),
        low_memory_stabilization_seconds=get_low_memory_stabilization_seconds(config),
    )
    return await _run_with_recipient_page(config, service.scan_dm_list)


async def resolve_recipients(
    config: Mapping[str, Any],
    recipients: Sequence[Mapping[str, Any]],
    *,
    on_progress: Callable[[int, int], None] | None = None,
    force: bool = False,
) -> list[dict[str, str]]:
    """Resolve display names with one short-lived shared browser session."""

    if not recipients:
        raise NoRecipientsSelectedError("Select at least one recipient before resolving usernames.")

    from daily_streak.recipients import RecipientService

    service = RecipientService(
        navigation_timeout_ms=get_navigation_timeout_ms(config),
        action_timeout_ms=get_action_timeout_ms(config),
        content_ready_timeout_ms=get_content_ready_timeout_ms(config),
        dm_readiness_timeout_ms=get_dm_readiness_timeout_ms(config),
        dm_load_attempts=get_dm_load_attempts(config),
        probe_timeout_ms=get_probe_timeout_ms(config),
        low_memory_mode=get_low_memory_mode(config),
        low_memory_stabilization_seconds=get_low_memory_stabilization_seconds(config),
    )

    async def resolve(page: Any) -> list[dict[str, str]]:
        return await service.resolve_usernames(
            page,
            recipients,
            on_progress=on_progress,
            force=force,
        )

    return await _run_with_recipient_page(config, resolve)
