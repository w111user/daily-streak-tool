"""Configuration file helpers shared by desktop and non-desktop adapters."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


DEFAULT_CONFIG_PATH = Path("config.json")

# These defaults intentionally match the legacy UI's behavior.  The values are
# returned through ``default_config`` so nested dictionaries are never shared.
DEFAULT_CONFIG: dict[str, Any] = {
    "schedule": {
        "time": "00:00",
        "timezone": "Asia/Ho_Chi_Minh",
        "run_on_start": True,
    },
    "cookie_file": "cookies.json",
    "tiktok": {
        "headless": False,
        "message_delay_seconds": [8, 18],
        "navigation_timeout_ms": 60000,
        "action_timeout_ms": 10000,
        "content_ready_timeout_ms": 45000,
        "dm_readiness_timeout_ms": 45000,
        "dm_load_attempts": 2,
        "probe_timeout_ms": 750,
        "low_memory_mode": False,
        "low_memory_stabilization_seconds": 4.0,
        "persistent_context": True,
        "recipient_retries": 1,
    },
    "recipients": [],
    "videos": [],
}


def default_config() -> dict[str, Any]:
    """Return an independent config dictionary with legacy-safe defaults."""

    return deepcopy(DEFAULT_CONFIG)


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    missing_ok: bool = False,
) -> dict[str, Any]:
    """Load a JSON configuration file.

    ``missing_ok`` is for first-run GUI behavior.  Scheduler and CLI callers
    should normally leave it false so a missing operational configuration is
    visible immediately.
    """

    config_path = Path(path)
    if not config_path.exists():
        if missing_ok:
            return default_config()
        raise FileNotFoundError(config_path)

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError(f"Configuration at {config_path} must be a JSON object.")
    return config


def save_config(config: Mapping[str, Any], path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Atomically write configuration in the legacy human-readable JSON format."""

    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(dict(config), file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, config_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
