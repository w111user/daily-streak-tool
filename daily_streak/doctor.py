"""Offline setup diagnostics for desktop and server installations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from daily_streak.browser import discover_browser_executable, get_user_data_dir
from daily_streak.config import load_config
from daily_streak.paths import get_data_dir
from daily_streak.schedule import schedule_from_config


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status.value, "message": self.message}


@dataclass(slots=True)
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    def add(self, name: str, status: CheckStatus, message: str) -> None:
        self.checks.append(DoctorCheck(name, status, message))

    @property
    def pass_count(self) -> int:
        return sum(check.status is CheckStatus.PASS for check in self.checks)

    @property
    def warn_count(self) -> int:
        return sum(check.status is CheckStatus.WARN for check in self.checks)

    @property
    def fail_count(self) -> int:
        return sum(check.status is CheckStatus.FAIL for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "summary": {
                "pass": self.pass_count,
                "warn": self.warn_count,
                "fail": self.fail_count,
            },
        }


def collect_doctor_report(config_path: str | Path) -> tuple[DoctorReport, dict[str, Any] | None]:
    """Inspect local configuration without starting a browser or sending data."""

    report = DoctorReport()
    python_status = CheckStatus.PASS if sys.version_info >= (3, 10) else CheckStatus.FAIL
    report.add(
        "python",
        python_status,
        f"Python {sys.version.split()[0]}" + ("" if python_status is CheckStatus.PASS else "; Python 3.10+ is required"),
    )

    path = Path(config_path)
    try:
        config = load_config(path)
    except FileNotFoundError:
        report.add("config", CheckStatus.FAIL, f"Configuration file not found: {path}")
        return report, None
    except Exception as exc:
        report.add("config", CheckStatus.FAIL, f"Configuration cannot be parsed: {exc}")
        return report, None
    report.add("config", CheckStatus.PASS, f"Configuration loaded from {path}")

    try:
        schedule = schedule_from_config(config)
    except ValueError as exc:
        report.add("schedule", CheckStatus.FAIL, str(exc))
    else:
        report.add(
            "schedule",
            CheckStatus.PASS,
            f"Daily at {schedule.time_text} {schedule.timezone}",
        )

    raw_recipients = config.get("recipients", [])
    recipients = raw_recipients if isinstance(raw_recipients, list) else []
    recipient_status = CheckStatus.PASS if recipients else CheckStatus.WARN
    report.add("recipients", recipient_status, f"{len(recipients)} configured")

    raw_videos = config.get("videos", [])
    videos = [item for item in raw_videos if isinstance(item, str) and item.strip()] if isinstance(raw_videos, list) else []
    video_status = CheckStatus.PASS if videos else CheckStatus.WARN
    report.add("videos", video_status, f"{len(videos)} configured")

    data_dir = get_data_dir(config)
    try:
        probe_directory = _nearest_existing_directory(data_dir)
        descriptor, temporary_name = tempfile.mkstemp(dir=probe_directory, prefix=".doctor-")
        os.close(descriptor)
        Path(temporary_name).unlink(missing_ok=True)
    except Exception as exc:
        report.add("data_dir", CheckStatus.FAIL, f"Not writable: {data_dir} ({exc})")
    else:
        if data_dir.exists():
            message = f"Writable: {data_dir}"
        else:
            message = f"Parent is writable; data directory can be created: {data_dir}"
        report.add("data_dir", CheckStatus.PASS, message)

    _add_cookie_check(report, config)
    _add_browser_checks(report, config)
    return report, config


def _add_cookie_check(report: DoctorReport, config: Mapping[str, Any]) -> None:
    cookie_path = Path(str(config.get("cookie_file", "cookies.json")))
    if not cookie_path.exists():
        report.add("cookies", CheckStatus.WARN, f"Cookie file not found: {cookie_path}")
        return
    try:
        value = json.loads(cookie_path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("cookie export must be a JSON list")
    except Exception as exc:
        report.add("cookies", CheckStatus.FAIL, f"Cookie file is not parseable: {exc}")
        return
    report.add("cookies", CheckStatus.PASS, f"Cookie export is parseable: {cookie_path}")


def _nearest_existing_directory(path: Path) -> Path:
    """Find a writable probe parent without creating the configured data path."""

    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    if not candidate.is_dir():
        raise NotADirectoryError(candidate)
    return candidate


def _add_browser_checks(report: DoctorReport, config: Mapping[str, Any]) -> None:
    raw_tiktok = config.get("tiktok", {})
    tiktok_config: Mapping[str, Any] = raw_tiktok if isinstance(raw_tiktok, Mapping) else {}
    preferred = tiktok_config.get("browser_executable") or tiktok_config.get("executable_path")
    executable = discover_browser_executable(preferred)
    if executable:
        report.add("browser", CheckStatus.PASS, f"Browser executable found: {executable}")
    else:
        report.add(
            "browser",
            CheckStatus.WARN,
            "No system Chrome/Chromium found; Playwright Chromium may be used if installed.",
        )

    playwright_available = importlib.util.find_spec("playwright") is not None
    report.add(
        "playwright",
        CheckStatus.PASS if playwright_available else CheckStatus.WARN,
        "Playwright package is available" if playwright_available else "Playwright package is not installed",
    )

    profile_dir = get_user_data_dir(tiktok_config)
    report.add("profile", CheckStatus.PASS, f"Browser profile directory: {profile_dir}")
    headless = bool(tiktok_config.get("headless", False))
    if headless:
        report.add("display", CheckStatus.PASS, "Headless browser mode is enabled")
    elif sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        report.add(
            "display",
            CheckStatus.WARN,
            "Headed browser mode has no DISPLAY; enable tiktok.headless or run under xvfb-run.",
        )
    else:
        report.add("display", CheckStatus.PASS, "Headed browser mode has a display environment")
