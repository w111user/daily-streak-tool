"""Per-user runtime data paths for Daily Streak Tool."""

from __future__ import annotations

import os
from pathlib import Path
import platform
from typing import Any, Mapping


APP_DATA_DIRECTORY_NAME = "daily-streak-tool"
DATA_DIRECTORY_ENV_VAR = "DAILY_STREAK_DATA_DIR"


def get_data_dir(
    config: Mapping[str, Any] | None = None,
    *,
    account_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Return the application data directory without creating it.

    ``DAILY_STREAK_DATA_DIR`` takes precedence for server deployment and tests.
    ``data_dir`` or ``state.data_dir`` config values are optional lower-priority
    overrides for callers that manage configuration centrally.

    When an ``account_name`` is specified (or configured via ``account_name`` in
    config), the data directory is strictly isolated under an ``accounts/<name>``
    subpath, ensuring independent runtime state across accounts even when
    ``DAILY_STREAK_DATA_DIR`` is set globally.
    """

    account = account_name
    if account is None and config is not None:
        raw_account = config.get("account_name") or config.get("account")
        if raw_account:
            account = str(raw_account).strip()

    environ = os.environ if environ is None else environ
    configured_path = environ.get(DATA_DIRECTORY_ENV_VAR)
    if not configured_path and config:
        configured_path = config.get("data_dir")
        state_config = config.get("state", {})
        if not configured_path and isinstance(state_config, Mapping):
            configured_path = state_config.get("data_dir")

    if configured_path:
        base_dir = Path(str(configured_path)).expanduser()
    else:
        system = system or platform.system()
        home_dir = home_dir or Path.home()
        if system == "Windows":
            local_app_data = environ.get("LOCALAPPDATA")
            platform_dir = Path(local_app_data) if local_app_data else home_dir / "AppData/Local"
        elif system == "Darwin":
            platform_dir = home_dir / "Library/Application Support"
        else:
            xdg_state_home = environ.get("XDG_STATE_HOME")
            platform_dir = Path(xdg_state_home).expanduser() if xdg_state_home else home_dir / ".local/state"
        base_dir = platform_dir / APP_DATA_DIRECTORY_NAME

    if account and account != "default":
        clean_account = str(account).strip()
        if clean_account and clean_account != "default":
            if base_dir.name == clean_account or (len(base_dir.parts) >= 2 and base_dir.parts[-2:] == ("accounts", clean_account)):
                return base_dir
            return base_dir / "accounts" / clean_account

    return base_dir


def ensure_data_dir(
    config: Mapping[str, Any] | None = None,
    *,
    account_name: str | None = None,
) -> Path:
    """Create and return the configured per-user data directory."""

    data_dir = get_data_dir(config, account_name=account_name)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
