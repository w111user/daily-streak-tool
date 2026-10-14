"""Shared Playwright browser/session configuration and cookie helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import time
from typing import Any, Callable, Mapping


logger = logging.getLogger(__name__)

DEFAULT_BROWSER_ARGS = (
    "--restore-last-session",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
)
DEFAULT_IGNORE_ARGS = ("--enable-automation", "--no-first-run")
LOW_MEMORY_BROWSER_ARGS = (
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
)
LOW_MEMORY_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
CHROMIUM_PROFILE_LOCK_FILENAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "LOCK",
)
DEFAULT_NAVIGATION_TIMEOUT_MS = 60_000
DEFAULT_ACTION_TIMEOUT_MS = 10_000
DEFAULT_CONTENT_READY_TIMEOUT_MS = 45_000
DEFAULT_DM_READINESS_TIMEOUT_MS = DEFAULT_CONTENT_READY_TIMEOUT_MS
DEFAULT_DM_LOAD_ATTEMPTS = 2
DEFAULT_PROBE_TIMEOUT_MS = 750
DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS = 4.0
EDIT_THIS_COOKIE_SAME_SITE_MAP = {
    "unspecified": "None",
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
}


@dataclass(frozen=True)
class BrowserLaunchConfig:
    """Resolved browser-session options, independent of Playwright types."""

    user_data_dir: Path | None
    executable_path: str | None
    headless: bool
    args: tuple[str, ...]
    ignore_default_args: tuple[str, ...]
    navigation_timeout_ms: int
    action_timeout_ms: int
    content_ready_timeout_ms: int
    probe_timeout_ms: int
    low_memory_mode: bool
    persistent_context: bool = True


@dataclass
class BrowserSession:
    """A browser/context pair with one owner for ordered asynchronous cleanup."""

    context: Any
    browser: Any | None
    persistent_context: bool
    page: Any | None = None

    async def get_page(self) -> Any:
        """Return one initial page after callers have injected cookies into the context."""

        if self.page is None:
            pages = getattr(self.context, "pages", ())
            self.page = pages[0] if pages else await self.context.new_page()
        return self.page

    async def load_cookies(self, cookie_file: str | Path) -> bool:
        """Load cookies through the shared converter and report setup failures clearly."""

        try:
            loaded = await load_cookies(self.context, cookie_file)
        except Exception as exc:
            logger.error("Cookie-loading failure: %s", exc)
            raise
        if loaded:
            logger.info("Cookies loaded")
        else:
            logger.warning("No cookies were loaded from %s", cookie_file)
        return loaded

    async def close(self) -> None:
        """Close page, context, then browser even when an earlier close fails."""

        close_error: BaseException | None = None
        for resource in (self.page, self.context, self.browser):
            if resource is None:
                continue
            try:
                await resource.close()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            raise close_error


def get_user_data_dir_key(system: str | None = None) -> str:
    """Return the legacy config key appropriate to the current platform."""

    system = system or platform.system()
    if system == "Windows":
        return "user_data_dir_windows"
    if system == "Darwin":
        return "user_data_dir_macos"
    return "user_data_dir_linux"


def get_default_user_data_dir(
    system: str | None = None,
    home_dir: Path | None = None,
) -> str:
    """Return the legacy default persistent browser-profile directory."""

    system = system or platform.system()
    home_dir = home_dir or Path.home()
    if system == "Windows":
        return str(home_dir / "AppData/Local/chrome-debug")
    if system == "Darwin":
        return str(home_dir / "Library/Application Support/chrome-debug")
    return str(home_dir / ".config/chrome-debug")


def get_user_data_dir(
    tiktok_config: Mapping[str, Any],
    *,
    system: str | None = None,
) -> Path:
    """Resolve the configured platform profile path without creating it."""

    configured_path = tiktok_config.get(get_user_data_dir_key(system))
    if configured_path:
        return Path(str(configured_path)).expanduser()
    return Path(get_default_user_data_dir(system)).expanduser()


def _candidate_browser_paths(system: str) -> tuple[Path, ...]:
    if system == "Windows":
        roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")]
        return tuple(
            Path(root) / "Google/Chrome/Application/chrome.exe"
            for root in roots
            if root
        ) + tuple(
            Path(root) / "Chromium/Application/chrome.exe"
            for root in roots
            if root
        )
    if system == "Darwin":
        return (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        )
    return (
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    )


def discover_browser_executable(
    preferred: str | Path | None = None,
    *,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    is_file: Callable[[Path], bool] | None = None,
) -> str | None:
    """Find a local Chrome/Chromium executable, or allow Playwright fallback.

    An optional configured path is used only when present.  If no system
    browser is found, callers omit ``executable_path`` so Playwright can use
    its installed Chromium build.
    """

    is_file = is_file or Path.is_file
    if preferred:
        preferred_path = Path(preferred).expanduser()
        if is_file(preferred_path):
            return str(preferred_path)
        command_path = which(str(preferred))
        if command_path:
            return command_path
        logger.warning("Configured browser executable was not found: %s", preferred_path)

    system = system or platform.system()
    for candidate in _candidate_browser_paths(system):
        if is_file(candidate):
            return str(candidate)

    if system == "Windows":
        command_names = ("chrome.exe", "chromium.exe", "chrome", "chromium")
    elif system == "Darwin":
        command_names = ("Google Chrome", "Chromium", "google-chrome", "chromium")
    else:
        command_names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for command in command_names:
        found = which(command)
        if found:
            return found
    return None


def get_chrome_path() -> str | None:
    """Compatibility wrapper for the former sender-level helper."""

    return discover_browser_executable()


def _configured_browser_args(
    tiktok_config: Mapping[str, Any],
    *,
    low_memory_mode: bool = False,
) -> tuple[str, ...]:
    configured_args = tiktok_config.get("browser_args", [])
    if not isinstance(configured_args, list) or not all(
        isinstance(argument, str) for argument in configured_args
    ):
        configured_args = []

    args = DEFAULT_BROWSER_ARGS + tuple(configured_args)
    if low_memory_mode:
        args += LOW_MEMORY_BROWSER_ARGS
    return args


def _configured_timeout_ms(
    tiktok_config: Mapping[str, Any],
    key: str,
    default: int,
) -> int:
    """Read a positive timeout while preserving legacy configs without it."""

    value = tiktok_config.get(key, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        logger.warning("Invalid tiktok.%s value %r; using %d ms.", key, value, default)
        return default


def _tiktok_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_tiktok_config = config.get("tiktok", {})
    return raw_tiktok_config if isinstance(raw_tiktok_config, Mapping) else {}


def get_action_timeout_ms(config: Mapping[str, Any]) -> int:
    """Return the configured ordinary Playwright action timeout."""

    return _configured_timeout_ms(
        _tiktok_config(config),
        "action_timeout_ms",
        DEFAULT_ACTION_TIMEOUT_MS,
    )


def get_navigation_timeout_ms(config: Mapping[str, Any]) -> int:
    """Return the configured finite timeout for page-navigation milestones."""

    return _configured_timeout_ms(
        _tiktok_config(config),
        "navigation_timeout_ms",
        DEFAULT_NAVIGATION_TIMEOUT_MS,
    )


def get_content_ready_timeout_ms(config: Mapping[str, Any]) -> int:
    """Return the bounded wait for substantial TikTok page content to load."""

    return _configured_timeout_ms(
        _tiktok_config(config),
        "content_ready_timeout_ms",
        DEFAULT_CONTENT_READY_TIMEOUT_MS,
    )


def get_dm_readiness_timeout_ms(config: Mapping[str, Any]) -> int:
    """Return the initial `/messages` hydration budget.

    Existing configurations used ``content_ready_timeout_ms`` for this same
    full readiness loop.  Retain that value as the fallback so adding the new
    key does not alter any current timeout behavior.
    """

    return _configured_timeout_ms(
        _tiktok_config(config),
        "dm_readiness_timeout_ms",
        get_content_ready_timeout_ms(config),
    )


def get_dm_load_attempts(config: Mapping[str, Any]) -> int:
    """Return the finite number of fresh-page DM load attempts."""

    value = _tiktok_config(config).get("dm_load_attempts", DEFAULT_DM_LOAD_ATTEMPTS)
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid tiktok.dm_load_attempts value %r; using %d.",
            value,
            DEFAULT_DM_LOAD_ATTEMPTS,
        )
        return DEFAULT_DM_LOAD_ATTEMPTS
    if attempts < 1:
        logger.warning(
            "tiktok.dm_load_attempts must be at least 1; using %d.",
            DEFAULT_DM_LOAD_ATTEMPTS,
        )
        return DEFAULT_DM_LOAD_ATTEMPTS
    return attempts


def get_probe_timeout_ms(config: Mapping[str, Any]) -> int:
    """Return a short bounded timeout for passive popup/CAPTCHA checks."""

    configured = _configured_timeout_ms(
        _tiktok_config(config),
        "probe_timeout_ms",
        DEFAULT_PROBE_TIMEOUT_MS,
    )
    bounded = min(1_000, max(500, configured))
    if bounded != configured:
        logger.warning(
            "tiktok.probe_timeout_ms must be between 500 and 1000 ms; using %d ms.",
            bounded,
        )
    return bounded


def get_low_memory_mode(config: Mapping[str, Any]) -> bool:
    """Return whether opt-in conservative Chromium resource filtering is enabled."""

    value = _tiktok_config(config).get("low_memory_mode", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def get_low_memory_stabilization_seconds(config: Mapping[str, Any]) -> float:
    """Return the optional post-context grace period for constrained renderers."""

    value = _tiktok_config(config).get(
        "low_memory_stabilization_seconds",
        DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS,
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid tiktok.low_memory_stabilization_seconds value %r; using %.1f seconds.",
            value,
            DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS,
        )
        return DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS


def get_persistent_context(config: Mapping[str, Any]) -> bool:
    """Return whether Chromium should use the legacy persistent-profile session."""

    value = _tiktok_config(config).get("persistent_context", True)
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


def get_browser_launch_config(config: Mapping[str, Any]) -> BrowserLaunchConfig:
    """Resolve all common persistent Chromium launch settings from config."""

    tiktok_config = _tiktok_config(config)
    preferred_executable = tiktok_config.get("browser_executable") or tiktok_config.get(
        "executable_path"
    )
    low_memory_mode = get_low_memory_mode(config)
    persistent_context = get_persistent_context(config)
    return BrowserLaunchConfig(
        user_data_dir=(get_user_data_dir(tiktok_config) if persistent_context else None),
        executable_path=discover_browser_executable(preferred_executable),
        headless=bool(tiktok_config.get("headless", False)),
        args=_configured_browser_args(tiktok_config, low_memory_mode=low_memory_mode),
        ignore_default_args=DEFAULT_IGNORE_ARGS,
        navigation_timeout_ms=_configured_timeout_ms(
            tiktok_config,
            "navigation_timeout_ms",
            DEFAULT_NAVIGATION_TIMEOUT_MS,
        ),
        action_timeout_ms=_configured_timeout_ms(
            tiktok_config,
            "action_timeout_ms",
            DEFAULT_ACTION_TIMEOUT_MS,
        ),
        content_ready_timeout_ms=_configured_timeout_ms(
            tiktok_config,
            "content_ready_timeout_ms",
            DEFAULT_CONTENT_READY_TIMEOUT_MS,
        ),
        probe_timeout_ms=get_probe_timeout_ms(config),
        low_memory_mode=low_memory_mode,
        persistent_context=persistent_context,
    )


async def low_memory_resource_filter(route: Any) -> None:
    """Block only media-heavy resource classes during an opt-in low-memory run."""

    request = route.request
    resource_type = str(getattr(request, "resource_type", "")).casefold()
    if resource_type in LOW_MEMORY_BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    await route.continue_()


async def install_low_memory_resource_filter(context: Any) -> None:
    """Install the narrow request filter used by low-memory Chromium mode."""

    await context.route("**/*", low_memory_resource_filter)


def _browser_launch_kwargs(
    launch_config: BrowserLaunchConfig,
    *,
    user_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Build shared Chromium options for persistent and ephemeral sessions."""

    launch_kwargs: dict[str, Any] = {
        "headless": launch_config.headless,
        "args": list(launch_config.args),
        "ignore_default_args": list(launch_config.ignore_default_args),
    }
    if user_data_dir is not None:
        launch_kwargs["user_data_dir"] = str(user_data_dir)
    if launch_config.executable_path:
        launch_kwargs["executable_path"] = launch_config.executable_path
    return launch_kwargs


async def _configure_context(context: Any, launch_config: BrowserLaunchConfig) -> None:
    """Apply shared timeouts and optional request filtering to a new context."""

    context.set_default_navigation_timeout(launch_config.navigation_timeout_ms)
    context.set_default_timeout(launch_config.action_timeout_ms)
    if launch_config.low_memory_mode:
        await install_low_memory_resource_filter(context)
        logger.info("Low-memory resource filtering enabled")


def warn_if_profile_lock_present(user_data_dir: Path) -> None:
    """Warn about profile lock artifacts without mutating the user's profile."""

    lock_files = [
        user_data_dir / name
        for name in CHROMIUM_PROFILE_LOCK_FILENAMES
        if (user_data_dir / name).exists() or (user_data_dir / name).is_symlink()
    ]
    if lock_files:
        logger.warning(
            "Chromium profile lock file(s) present in %s: %s. "
            "If no Chromium process uses this profile, they may be stale; they were not removed.",
            user_data_dir,
            ", ".join(lock_file.name for lock_file in lock_files),
        )


async def launch_persistent_context(playwright: Any, config: Mapping[str, Any]) -> Any:
    """Launch the legacy configured persistent context with shared setup.

    This compatibility function intentionally remains persistent even when a
    caller's config opts into ephemeral sessions.  New shared callers should
    use :func:`launch_browser_session` so they honor ``persistent_context``.
    """

    launch_config = get_browser_launch_config(config)
    user_data_dir = launch_config.user_data_dir or get_user_data_dir(_tiktok_config(config))
    warn_if_profile_lock_present(user_data_dir)
    logger.info("Launching Chromium (persistent)")
    logger.info("Creating browser context from persistent Chromium profile")
    start_time = time.monotonic()
    try:
        context = await playwright.chromium.launch_persistent_context(
            **_browser_launch_kwargs(launch_config, user_data_dir=user_data_dir)
        )
    except Exception as exc:
        elapsed_seconds = time.monotonic() - start_time
        if _is_playwright_timeout(exc):
            logger.error(
                "Persistent Chromium launch/context timeout after %.2fs; "
                "this can indicate browser startup or profile initialization.",
                elapsed_seconds,
            )
        else:
            logger.error("Chromium persistent launch failure: %s", exc)
        raise

    elapsed_seconds = time.monotonic() - start_time
    logger.info("Chromium launched in %.2fs", elapsed_seconds)
    try:
        await _configure_context(context, launch_config)
        logger.info("Browser context ready in %.2fs", elapsed_seconds)
        return context
    except Exception:
        await context.close()
        raise


async def launch_browser_session(
    playwright: Any,
    config: Mapping[str, Any],
) -> BrowserSession:
    """Launch the configured persistent or ephemeral browser/session pair."""

    launch_config = get_browser_launch_config(config)
    if launch_config.persistent_context:
        context = await launch_persistent_context(playwright, config)
        return BrowserSession(context=context, browser=None, persistent_context=True)

    logger.info("Launching Chromium (ephemeral)")
    launch_started_at = time.monotonic()
    try:
        browser = await playwright.chromium.launch(
            **_browser_launch_kwargs(launch_config)
        )
    except Exception as exc:
        elapsed_seconds = time.monotonic() - launch_started_at
        if _is_playwright_timeout(exc):
            logger.error(
                "Browser process launch timeout while launching Chromium (ephemeral) after %.2fs",
                elapsed_seconds,
            )
        else:
            logger.error("Chromium ephemeral launch failure: %s", exc)
        raise

    logger.info("Chromium launched in %.2fs", time.monotonic() - launch_started_at)
    logger.info("Creating browser context")
    context_started_at = time.monotonic()
    try:
        context = await browser.new_context()
    except Exception as exc:
        elapsed_seconds = time.monotonic() - context_started_at
        if _is_playwright_timeout(exc):
            logger.error("Browser context creation timeout after %.2fs", elapsed_seconds)
        else:
            logger.error("Browser context creation failure: %s", exc)
        await browser.close()
        raise

    try:
        await _configure_context(context, launch_config)
    except Exception:
        await context.close()
        await browser.close()
        raise
    logger.info("Browser context ready in %.2fs", time.monotonic() - context_started_at)
    return BrowserSession(context=context, browser=browser, persistent_context=False)


def _is_playwright_timeout(error: Exception) -> bool:
    """Avoid importing Playwright at module import time solely for diagnostics."""

    return error.__class__.__name__ == "TimeoutError" or "timeout" in str(error).casefold()


def check_cookies_valid(cookie_file: str | Path) -> bool:
    """Check that an exported ``sessionid`` cookie has not expired."""

    cookie_path = Path(cookie_file)
    if not cookie_path.exists():
        return False

    try:
        with cookie_path.open("r", encoding="utf-8") as file:
            cookies = json.load(file)
        if not isinstance(cookies, list):
            raise ValueError("Cookie export must be a JSON list")
    except Exception:
        logger.exception("Failed to load cookies from %s", cookie_path)
        return False

    for cookie in cookies:
        if not isinstance(cookie, Mapping) or cookie.get("name") != "sessionid":
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if expires is None:
            return False
        try:
            return time.time() < float(expires)
        except (TypeError, ValueError):
            logger.warning("Invalid sessionid expiry timestamp in %s: %r", cookie_path, expires)
            return False
    return False


def convert_cookies(cookies: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Convert EditThisCookie exports to Playwright cookies when necessary."""

    if any("expirationDate" in cookie for cookie in cookies):
        converted = []
        for cookie in cookies:
            if cookie.get("session", False):
                continue
            converted.append(
                {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie["domain"],
                    "path": cookie.get("path", "/"),
                    "expires": int(cookie["expirationDate"]),
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", False),
                    "sameSite": EDIT_THIS_COOKIE_SAME_SITE_MAP.get(
                        cookie.get("sameSite", "unspecified"), "None"
                    ),
                }
            )
        return converted, "EditThisCookie"
    if any("expires" in cookie for cookie in cookies):
        return [dict(cookie) for cookie in cookies], "Playwright"
    return [dict(cookie) for cookie in cookies], "unknown"


async def load_cookies(context: Any, cookie_file: str | Path) -> bool:
    """Read, normalize, and inject cookies into a Playwright context."""

    cookie_path = Path(cookie_file)
    if not cookie_path.exists():
        return False

    with cookie_path.open("r", encoding="utf-8") as file:
        raw_cookies = json.load(file)
    if not isinstance(raw_cookies, list) or not all(
        isinstance(cookie, Mapping) for cookie in raw_cookies
    ):
        raise ValueError(f"Cookie export at {cookie_path} must be a JSON list of objects.")
    cookies, cookie_format = convert_cookies(raw_cookies)
    await context.add_cookies(cookies)
    logger.info(
        "Detected %s cookie format and loaded %d cookies from %s",
        cookie_format,
        len(cookies),
        cookie_path,
    )
    return True
