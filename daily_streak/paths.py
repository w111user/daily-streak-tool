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
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Return the application data directory without creating it.

    ``DAILY_STREAK_DATA_DIR`` takes precedence for server deployment and tests.
    ``data_dir`` or ``state.data_dir`` config values are optional lower-priority
    overrides for callers that manage configuration centrally.
    """

    environ = os.environ if environ is None else environ
    configured_path = environ.get(DATA_DIRECTORY_ENV_VAR)
    if not configured_path and config:
        configured_path = config.get("data_dir")
        state_config = config.get("state", {})
        if not configured_path and isinstance(state_config, Mapping):
            configured_path = state_config.get("data_dir")
    if configured_path:
        return Path(str(configured_path)).expanduser()

    system = system or platform.system()
    home_dir = home_dir or Path.home()
    if system == "Windows":
        local_app_data = environ.get("LOCALAPPDATA")
        base_dir = Path(local_app_data) if local_app_data else home_dir / "AppData/Local"
    elif system == "Darwin":
        base_dir = home_dir / "Library/Application Support"
    else:
        xdg_state_home = environ.get("XDG_STATE_HOME")
        base_dir = Path(xdg_state_home).expanduser() if xdg_state_home else home_dir / ".local/state"
    return base_dir / APP_DATA_DIRECTORY_NAME


def ensure_data_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Create and return the configured per-user data directory."""

    data_dir = get_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
