from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from daily_streak.config import default_config, load_config, save_config


class ConfigTests(unittest.TestCase):
    def test_missing_first_run_config_uses_independent_defaults(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "missing.json"
            first = load_config(path, missing_ok=True)
            second = load_config(path, missing_ok=True)

        first["tiktok"]["message_delay_seconds"][0] = 99
        self.assertEqual(second["tiktok"]["message_delay_seconds"], [8, 18])
        self.assertEqual(second["tiktok"]["dm_readiness_timeout_ms"], 45_000)
        self.assertEqual(second["tiktok"]["dm_load_attempts"], 2)

    def test_missing_operational_config_raises(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                load_config(path)

    def test_load_and_save_preserves_existing_config_shape(self) -> None:
        config = default_config()
        config["future_setting"] = {"preserved": True}
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            save_config(config, path)
            loaded = load_config(path)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded, config)
        self.assertEqual(raw["future_setting"], {"preserved": True})

    def test_non_object_json_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
