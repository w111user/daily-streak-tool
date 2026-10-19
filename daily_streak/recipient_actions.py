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


async def refresh_streaks(
    config: Mapping[str, Any],
    recipients: Sequence[Mapping[str, Any]],
    *,
    force: bool = True,
    device_id: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    reader_factory: Callable[..., Any] | None = None,
    streak_lock_factory: Callable[[Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Refresh TikTok streak statuses for recipients via standalone API or Android UI reader.

    Updates the shared durable StreakCache and returns structured recipient records.
    Serialized across processes via StreakLock.
    """

    if not recipients:
        return []

    import asyncio
    from daily_streak.lock import StreakLock
    from daily_streak.paths import get_data_dir
    from daily_streak.streak_reader import StreakCache, format_streak_display

    data_dir = get_data_dir(config)
    cache = StreakCache(data_dir)
    lock = streak_lock_factory(data_dir) if streak_lock_factory is not None else StreakLock(data_dir)
    raw_android = config.get("android", {})
    android_cfg = raw_android if isinstance(raw_android, Mapping) else {}
    target_device = device_id or android_cfg.get("device_id") or config.get("device_id")

    if reader_factory is not None:
        reader = reader_factory(cache, target_device)
    elif device_id is not None or android_cfg.get("use_android"):
        from daily_streak.android_streak_reader import AndroidAdbClient, AndroidStreakReader

        adb_path = str(android_cfg.get("adb_path") or "adb")
        adb_client = AndroidAdbClient(device_id=target_device, adb_path=adb_path)
        reader = AndroidStreakReader(cache, adb_client=adb_client)
    else:
        from daily_streak.api_streak_reader import ApiStreakReader

        cookie_file = config.get("cookie_file", "cookies.json")
        reader = ApiStreakReader(cache, cookie_file=cookie_file)

    def _sync_refresh() -> list[dict[str, Any]]:
        lock.acquire()
        try:
            if hasattr(reader, "refresh_recipients"):
                records = reader.refresh_recipients(
                    recipients,
                    force_refresh=force,
                    populate_cache=True,
                    on_progress=on_progress,
                )
                results: list[dict[str, Any]] = []
                for item, record in zip(recipients, records):
                    name = str(item.get("name") or "").strip()
                    username = str(item.get("username") or "").strip()
                    results.append(
                        {
                            "name": name,
                            "username": username,
                            "streak": record.to_dict(),
                            "streak_display": format_streak_display(record),
                        }
                    )
                return results

            # Scan visible Android screen once and populate cache
            items = getattr(reader, "scan_inbox_streaks", lambda **kw: [])(populate_cache=True)
            results = []
            total = len(recipients)
            for index, item in enumerate(recipients, start=1):
                name = str(item.get("name") or "").strip()
                username = str(item.get("username") or "").strip()
                if not username:
                    results.append(
                        {
                            "name": name,
                            "username": "",
                            "streak": None,
                            "streak_display": "Chưa xác định",
                        }
                    )
                    continue
                record = reader.read_recipient_streak(
                    username,
                    display_name=name or None,
                    force_refresh=force,
                    items=items,
                )
                results.append(
                    {
                        "name": name,
                        "username": username,
                        "streak": record.to_dict(),
                        "streak_display": format_streak_display(record),
                    }
                )
                if on_progress is not None:
                    on_progress(index, total)
            return results
        finally:
            lock.release()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_refresh)
