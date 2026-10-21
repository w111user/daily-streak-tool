"""Named multi-account instance and lifecycle management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

from daily_streak.config import DEFAULT_CONFIG_PATH, load_config, save_config
from daily_streak.daemon import DaemonManager, DaemonService, create_daemon_service
from daily_streak.paths import ensure_data_dir, get_data_dir


logger = logging.getLogger(__name__)

ACCOUNT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
DEFAULT_ACCOUNTS_DIRNAME = "accounts"

ConfigLoader = Callable[[str | Path], dict[str, Any]]
ConfigSaver = Callable[[Mapping[str, Any], str | Path], None]


def validate_account_name(name: str) -> str:
    """Validate that an account name contains only safe path characters."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Account name cannot be empty.")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Account name '{cleaned}' contains invalid path characters.")
    if not ACCOUNT_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid account name '{cleaned}'. Account names must start with an alphanumeric character "
            "and contain only letters, numbers, hyphens, underscores, or dots (max 64 characters)."
        )
    return cleaned


@dataclass
class AccountInstance:
    """Represents a configured named account instance."""

    name: str
    config_path: Path
    data_dir: Path
    cookie_file: Path | None = None
    enabled: bool = True
    account_config: dict[str, Any] = field(default_factory=dict)

    @property
    def schedule_time(self) -> str:
        sched = self.account_config.get("schedule", {})
        if isinstance(sched, Mapping):
            return str(sched.get("time", "00:00"))
        return "00:00"

    @property
    def schedule_timezone(self) -> str:
        sched = self.account_config.get("schedule", {})
        if isinstance(sched, Mapping):
            return str(sched.get("timezone", "Asia/Ho_Chi_Minh"))
        return "Asia/Ho_Chi_Minh"


class AccountManager:
    """Discovers, creates, and controls isolated named account instances."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        accounts_dir: str | Path | None = None,
        default_config_path: str | Path = DEFAULT_CONFIG_PATH,
        config_loader: ConfigLoader = load_config,
        config_saver: ConfigSaver = save_config,
    ) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()

        cfg_p = Path(default_config_path)
        if cfg_p.is_absolute():
            self.default_config_path = cfg_p.resolve()
        else:
            self.default_config_path = (self.base_dir / cfg_p).resolve()

        if accounts_dir:
            acc_p = Path(accounts_dir)
            if acc_p.is_absolute():
                self.accounts_dir = acc_p.resolve()
            else:
                self.accounts_dir = (self.base_dir / acc_p).resolve()
        else:
            self.accounts_dir = (self.base_dir / DEFAULT_ACCOUNTS_DIRNAME).resolve()

        self.config_loader = config_loader
        self.config_saver = config_saver

    def _resolve_cookie_file(self, config: Mapping[str, Any], config_path: Path) -> Path | None:
        cookie_val = config.get("cookie_file")
        if not cookie_val:
            return None
        cookie_str = str(cookie_val).strip()
        p = Path(cookie_str).expanduser()
        if p.is_absolute():
            return p.resolve()
        # Relative to the directory containing the config file
        return (config_path.parent / p).resolve()

    def _load_instance(self, name: str, config_path: Path) -> AccountInstance:
        try:
            cfg = self.config_loader(config_path)
        except Exception as exc:
            logger.warning("Failed to load config for account %s at %s: %s", name, config_path, exc)
            cfg = {}
        data_dir = get_data_dir(cfg, account_name=name)
        cookie_path = self._resolve_cookie_file(cfg, config_path)
        enabled = bool(cfg.get("enabled", True))
        return AccountInstance(
            name=name,
            config_path=config_path,
            data_dir=data_dir,
            cookie_file=cookie_path,
            enabled=enabled,
            account_config=cfg,
        )

    def list_accounts(self, *, include_default: bool = False) -> list[AccountInstance]:
        """Discover all configured accounts.

        If named accounts exist, returns them.
        If no named accounts exist or ``include_default=True``, also includes
        the root default config instance for single-account backwards compatibility.
        """
        accounts: dict[str, AccountInstance] = {}

        # 1. Look in accounts_dir (<base_dir>/accounts)
        if self.accounts_dir.exists() and self.accounts_dir.is_dir():
            for child in sorted(self.accounts_dir.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    acc_config = child / "config.json"
                    if acc_config.exists() and acc_config.is_file():
                        accounts[child.name] = self._load_instance(child.name, acc_config)
                elif child.is_file() and child.suffix == ".json" and not child.name.startswith("."):
                    acc_name = child.stem
                    if acc_name not in accounts:
                        accounts[acc_name] = self._load_instance(acc_name, child)

        # 2. Look in root config.json under "accounts" key
        if self.default_config_path.exists():
            try:
                root_cfg = self.config_loader(self.default_config_path)
                raw_accounts = root_cfg.get("accounts")
                if isinstance(raw_accounts, Mapping):
                    for acc_name, acc_entry in raw_accounts.items():
                        if acc_name not in accounts:
                            if isinstance(acc_entry, Mapping) and "config_path" in acc_entry:
                                cfg_p = Path(str(acc_entry["config_path"])).expanduser()
                                if not cfg_p.is_absolute():
                                    cfg_p = (self.default_config_path.parent / cfg_p).resolve()
                                accounts[acc_name] = self._load_instance(acc_name, cfg_p)
            except Exception:
                pass

        # 3. Look in data_dir accounts
        try:
            default_data_dir = get_data_dir()
            data_accounts_dir = default_data_dir / "accounts"
            if data_accounts_dir.exists() and data_accounts_dir.is_dir():
                for child in sorted(data_accounts_dir.iterdir()):
                    if child.is_dir() and child.name not in accounts and not child.name.startswith("."):
                        acc_config = child / "config.json"
                        if acc_config.exists():
                            accounts[child.name] = self._load_instance(child.name, acc_config)
        except Exception:
            pass

        # If no named accounts found, or caller asked for default
        if not accounts or include_default:
            if self.default_config_path.exists():
                default_inst = self._load_instance("default", self.default_config_path)
                if not accounts:
                    return [default_inst]
                if include_default and "default" not in accounts:
                    return [default_inst] + list(accounts.values())

        return list(accounts.values())

    def get_account(self, name: str) -> AccountInstance:
        """Retrieve an account by name."""
        cleaned = validate_account_name(name)
        accounts = {acc.name: acc for acc in self.list_accounts(include_default=True)}
        if cleaned in accounts:
            return accounts[cleaned]

        # Check direct path in accounts_dir even if not discovered
        direct_config = self.accounts_dir / cleaned / "config.json"
        if direct_config.exists():
            return self._load_instance(cleaned, direct_config)

        direct_json = self.accounts_dir / f"{cleaned}.json"
        if direct_json.exists():
            return self._load_instance(cleaned, direct_json)

        raise ValueError(f"Account '{cleaned}' not found.")

    def create_account(
        self,
        name: str,
        *,
        copy_from: str | Path | None = None,
        cookie_file: str | Path | None = None,
        schedule_time: str | None = None,
        schedule_timezone: str | None = None,
        run_on_start: bool = False,
        message_format: str = "native_video_card",
        allow_raw_url_fallback: bool = False,
    ) -> AccountInstance:
        """Create a new isolated named account."""
        clean_name = validate_account_name(name)
        try:
            self.get_account(clean_name)
            raise ValueError(f"Account '{clean_name}' already exists.")
        except ValueError as err:
            if "already exists" in str(err):
                raise

        account_dir = self.accounts_dir / clean_name
        account_dir.mkdir(parents=True, exist_ok=True)
        config_path = (account_dir / "config.json").resolve()

        # Determine base configuration
        base_config: dict[str, Any] = {}
        if copy_from:
            copy_path: Path
            if isinstance(copy_from, Path) or (isinstance(copy_from, str) and ("/" in copy_from or "\\" in copy_from)):
                copy_path = Path(copy_from).resolve()
            else:
                existing = self.get_account(str(copy_from))
                copy_path = existing.config_path
            base_config = dict(self.config_loader(copy_path))
        elif self.default_config_path.exists():
            base_config = dict(self.config_loader(self.default_config_path))
        else:
            base_config = {
                "schedule": {
                    "time": "00:00",
                    "timezone": "Asia/Ho_Chi_Minh",
                    "run_on_start": False,
                },
                "tiktok": {
                    "headless": False,
                    "message_format": "native_video_card",
                    "allow_raw_url_fallback": False,
                },
                "recipients": [],
                "videos": [],
            }

        # Apply strict isolation overrides
        base_config["account_name"] = clean_name

        # Ensure independent, absolute cookie_file path
        if cookie_file:
            resolved_cookie = Path(cookie_file).expanduser().resolve()
            base_config["cookie_file"] = str(resolved_cookie)
        else:
            # Default to an isolated cookie file inside the account directory
            isolated_cookie = (account_dir / "cookies.json").resolve()
            base_config["cookie_file"] = str(isolated_cookie)

        # Schedule overrides
        sched = dict(base_config.get("schedule", {})) if isinstance(base_config.get("schedule"), Mapping) else {}
        if schedule_time:
            sched["time"] = schedule_time
        if schedule_timezone:
            sched["timezone"] = schedule_timezone
        sched["run_on_start"] = bool(run_on_start)
        base_config["schedule"] = sched

        # TikTok safety settings
        tiktok = dict(base_config.get("tiktok", {})) if isinstance(base_config.get("tiktok"), Mapping) else {}
        tiktok["message_format"] = message_format or "native_video_card"
        tiktok["allow_raw_url_fallback"] = bool(allow_raw_url_fallback)
        tiktok["account_name"] = clean_name
        base_config["tiktok"] = tiktok

        # Disable Telegram if copied to prevent accidental bot token conflicts
        if "telegram" in base_config and isinstance(base_config["telegram"], Mapping):
            telegram_cfg = dict(base_config["telegram"])
            if copy_from:
                telegram_cfg["enabled"] = False
            base_config["telegram"] = telegram_cfg

        # Save config
        self.config_saver(base_config, config_path)
        ensure_data_dir(base_config, account_name=clean_name)
        return self._load_instance(clean_name, config_path)

    def delete_account(self, name: str, *, remove_data: bool = False) -> bool:
        """Stop and delete a named account."""
        account = self.get_account(name)
        if account.name == "default" and account.config_path == self.default_config_path:
            raise ValueError("Cannot delete the root default account.")

        # Stop daemon first if active
        try:
            self.stop_daemon(account.name, timeout=5.0)
        except Exception:
            pass

        account_dir = account.config_path.parent
        if account_dir.name == account.name and account_dir.is_dir():
            shutil.rmtree(account_dir, ignore_errors=True)
        elif account.config_path.exists():
            account.config_path.unlink(missing_ok=True)

        if remove_data and account.data_dir.exists() and account.data_dir.is_dir():
            shutil.rmtree(account.data_dir, ignore_errors=True)

        return True

    def check_conflicts(
        self,
        accounts: Sequence[AccountInstance] | None = None,
    ) -> list[dict[str, Any]]:
        """Inspect accounts for schedule collisions, shared cookie files, and Telegram bot conflicts."""
        acc_list = list(accounts if accounts is not None else self.list_accounts())
        if len(acc_list) < 2:
            return []

        conflicts: list[dict[str, Any]] = []

        # 1. Schedule collision check
        schedules: dict[tuple[str, str], list[str]] = {}
        for acc in acc_list:
            if not acc.enabled:
                continue
            key = (acc.schedule_time, acc.schedule_timezone)
            schedules.setdefault(key, []).append(acc.name)

        for (time_val, tz_val), names in schedules.items():
            if len(names) > 1:
                conflicts.append(
                    {
                        "type": "schedule",
                        "severity": "warning",
                        "accounts": names,
                        "message": (
                            f"Schedule collision: accounts {', '.join(names)} are all scheduled for "
                            f"{time_val} ({tz_val}). Concurrent runs may cause resource contention."
                        ),
                    }
                )

        # 2. Cookie file collision check
        cookie_files: dict[Path, list[str]] = {}
        for acc in acc_list:
            if not acc.enabled or not acc.cookie_file:
                continue
            cookie_files.setdefault(acc.cookie_file.resolve(), []).append(acc.name)

        for cookie_p, names in cookie_files.items():
            if len(names) > 1:
                conflicts.append(
                    {
                        "type": "cookie",
                        "severity": "warning",
                        "accounts": names,
                        "message": (
                            f"Cookie collision: accounts {', '.join(names)} share the same cookie file "
                            f"({cookie_p}). Separate cookie files must be used to avoid session overwrite."
                        ),
                    }
                )

        # 3. Telegram bot token collision check
        telegram_tokens: dict[str, list[str]] = {}
        for acc in acc_list:
            if not acc.enabled:
                continue
            tg = acc.account_config.get("telegram")
            if isinstance(tg, Mapping) and tg.get("enabled"):
                token = str(tg.get("bot_token") or "").strip()
                if token and token != "PASTE_BOT_TOKEN_HERE":
                    telegram_tokens.setdefault(token, []).append(acc.name)

        for token, names in telegram_tokens.items():
            if len(names) > 1:
                masked = f"...{token[-6:]}" if len(token) >= 6 else "...***"
                conflicts.append(
                    {
                        "type": "telegram",
                        "severity": "warning",
                        "accounts": names,
                        "message": (
                            f"Telegram token collision: accounts {', '.join(names)} share the same bot token "
                            f"({masked}). Concurrent pollers will encounter HTTP 409 Conflict."
                        ),
                    }
                )

        return conflicts

    def get_daemon_manager(self, name: str) -> DaemonManager:
        """Construct DaemonManager isolated to the specified account."""
        account = self.get_account(name)
        return DaemonManager(
            config_path=account.config_path,
            account_name=account.name,
            config_loader=self.config_loader,
        )

    def get_daemon_service(self, name: str) -> DaemonService:
        """Construct DaemonService isolated to the specified account."""
        account = self.get_account(name)
        return create_daemon_service(
            account.account_config,
            config_path=account.config_path,
            account_name=account.name,
            config_loader=self.config_loader,
        )

    def get_status(self, name: str) -> dict[str, Any]:
        """Query daemon status for a named account."""
        account = self.get_account(name)
        mgr = self.get_daemon_manager(name)
        status = mgr.get_status()
        status["account_name"] = account.name
        status["config_path"] = account.config_path
        status["cookie_file"] = account.cookie_file
        return status

    def get_all_statuses(self) -> list[dict[str, Any]]:
        """Query status for all accounts."""
        results: list[dict[str, Any]] = []
        for account in self.list_accounts():
            try:
                results.append(self.get_status(account.name))
            except Exception as exc:
                results.append(
                    {
                        "account_name": account.name,
                        "status": f"Error: {exc}",
                        "is_running": False,
                        "pid": None,
                        "next_run": "N/A",
                        "schedule_time": account.schedule_time,
                        "timezone": account.schedule_timezone,
                        "config_path": account.config_path,
                    }
                )
        return results

    def start_daemon(self, name: str) -> int:
        """Start daemon for the named account."""
        mgr = self.get_daemon_manager(name)
        return mgr.start()

    def stop_daemon(self, name: str, timeout: float = 10.0) -> bool:
        """Stop daemon for the named account."""
        mgr = self.get_daemon_manager(name)
        return mgr.stop(timeout=timeout)

    def read_logs(self, name: str, max_lines: int = 100) -> list[str]:
        """Read daemon logs for the named account."""
        mgr = self.get_daemon_manager(name)
        return mgr.read_logs(max_lines=max_lines)
