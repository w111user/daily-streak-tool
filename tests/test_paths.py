from __future__ import annotations

from pathlib import Path
import unittest

from daily_streak.paths import DATA_DIRECTORY_ENV_VAR, get_data_dir


class DataPathTests(unittest.TestCase):
    def test_linux_default_uses_xdg_state_location(self) -> None:
        data_dir = get_data_dir(
            environ={},
            system="Linux",
            home_dir=Path("/home/tester"),
        )
        self.assertEqual(data_dir, Path("/home/tester/.local/state/daily-streak-tool"))

    def test_environment_override_has_priority_over_config(self) -> None:
        data_dir = get_data_dir(
            {"data_dir": "/configured/path"},
            environ={DATA_DIRECTORY_ENV_VAR: "/environment/path"},
            system="Linux",
            home_dir=Path("/home/tester"),
        )
        self.assertEqual(data_dir, Path("/environment/path"))

    def test_macos_and_windows_defaults(self) -> None:
        home = Path("/home/tester")
        self.assertEqual(
            get_data_dir(environ={}, system="Darwin", home_dir=home),
            home / "Library/Application Support/daily-streak-tool",
        )
        self.assertEqual(
            get_data_dir(
                environ={"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
                system="Windows",
                home_dir=home,
            ),
            Path("C:/Users/tester/AppData/Local/daily-streak-tool"),
        )
