"""Unit tests for official TikTok Android UI streak reader."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock

from daily_streak.android_streak_reader import (
    AndroidAdbClient,
    AndroidConversationItem,
    AndroidStreakReader,
    parse_inbox_hierarchy,
)
from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus

SAMPLE_INBOX_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.ss.android.ugc.trill">
    <!-- Conversation 1: HuyLong with 269 streak -->
    <node index="0" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,1845][1440,2097]">
      <node index="0" text="" resource-id="com.ss.android.ugc.trill:id/bpo" class="android.widget.RelativeLayout" />
      <node index="1" text="\u200eHuyLong" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[294,1899][501,1969]" />
      <node index="2" text="269" resource-id="" class="android.widget.TextView" bounds="[574,1900][661,1967]" />
      <node index="3" text="\u200eActive now" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" bounds="[294,1980][534,2043]" />
    </node>
    <!-- Conversation 2: Khoa Duc without streak -->
    <node index="1" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,2601][1440,2853]">
      <node index="1" text="\u200eKhoa Đức" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[294,2655][521,2725]" />
      <node index="2" text="\u200eSeen 2h ago" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" bounds="[294,2736][564,2799]" />
    </node>
    <!-- Conversation 3: En Nho with 71 streak -->
    <node index="2" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,1200][1440,1450]">
      <node index="1" text="\u200eÉn nhỏ" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[294,1220][450,1280]" />
      <node index="2" text="71" resource-id="" class="android.widget.TextView" bounds="[500,1220][580,1280]" />
      <node index="3" text="Video" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" />
    </node>
    <!-- Conversation 4: Ended streak -->
    <node index="3" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,1450][1440,1700]">
      <node index="1" text="Old Friend" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" />
      <node index="2" text="Streak lost yesterday" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" />
    </node>
  </node>
</hierarchy>
"""


class TestAndroidInboxXmlParser(unittest.TestCase):
    """Test parse_inbox_hierarchy with various XML payloads."""

    def test_parse_valid_hierarchy(self) -> None:
        items = parse_inbox_hierarchy(SAMPLE_INBOX_XML)
        self.assertEqual(len(items), 4)

        # 1. HuyLong: 269
        huylong = next(i for i in items if i.display_name == "HuyLong")
        self.assertEqual(huylong.status, StreakStatus.AVAILABLE)
        self.assertEqual(huylong.count, 269)
        self.assertEqual(huylong.subtitle, "Active now")

        # 2. Khoa Duc: not_visible
        khoa = next(i for i in items if i.display_name == "Khoa Đức")
        self.assertEqual(khoa.status, StreakStatus.NOT_VISIBLE)
        self.assertIsNone(khoa.count)
        self.assertEqual(khoa.subtitle, "Seen 2h ago")

        # 3. En Nho: 71
        en = next(i for i in items if i.display_name == "Én nhỏ")
        self.assertEqual(en.status, StreakStatus.AVAILABLE)
        self.assertEqual(en.count, 71)

        # 4. Old Friend: not_active
        old = next(i for i in items if i.display_name == "Old Friend")
        self.assertEqual(old.status, StreakStatus.NOT_ACTIVE)
        self.assertIsNone(old.count)

    def test_parse_empty_or_whitespace_xml(self) -> None:
        self.assertEqual(parse_inbox_hierarchy(""), [])
        self.assertEqual(parse_inbox_hierarchy("   \n\t  "), [])

    def test_parse_malformed_xml_returns_empty(self) -> None:
        self.assertEqual(parse_inbox_hierarchy("<broken xml><unclosed"), [])

    def test_parse_handles_leading_adb_stdout_text(self) -> None:
        prefixed_xml = "UI hierchary dumped to: /data/local/tmp/dump.xml\n" + SAMPLE_INBOX_XML
        items = parse_inbox_hierarchy(prefixed_xml)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0].display_name, "HuyLong")


class TestAndroidAdbClient(unittest.TestCase):
    """Test AndroidAdbClient command execution and error handling."""

    def test_build_cmd_with_device_id(self) -> None:
        client = AndroidAdbClient(device_id="device123", adb_path="/usr/bin/adb")
        cmd = client._build_cmd("shell", "getprop")
        self.assertEqual(cmd, ["/usr/bin/adb", "-s", "device123", "shell", "getprop"])

    def test_build_cmd_without_device_id(self) -> None:
        client = AndroidAdbClient(device_id=None, adb_path="adb")
        cmd = client._build_cmd("devices")
        self.assertEqual(cmd, ["adb", "devices"])

    def test_run_cmd_success(self) -> None:
        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb"], returncode=0, stdout="Success output", stderr=""
        ))
        client = AndroidAdbClient(command_runner=mock_runner)
        out = client.run_cmd("shell", "echo 1")
        self.assertEqual(out, "Success output")

    def test_run_cmd_failure_raises_runtime_error(self) -> None:
        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb"], returncode=1, stdout="", stderr="device not found"
        ))
        client = AndroidAdbClient(command_runner=mock_runner)
        with self.assertRaises(RuntimeError) as ctx:
            client.run_cmd("shell", "ls")
        self.assertIn("device not found", str(ctx.exception))


class TestAndroidStreakReader(unittest.TestCase):
    """Test AndroidStreakReader mapping, caching, and fail-safe behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.cache = StreakCache(self.data_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_streak_matches_by_display_name(self) -> None:
        mock_adb = MagicMock()
        mock_adb.dump_window_hierarchy.return_value = SAMPLE_INBOX_XML
        reader = AndroidStreakReader(self.cache, adb_client=mock_adb)

        record = reader.read_recipient_streak("huylong276", display_name="HuyLong")
        self.assertEqual(record.status, StreakStatus.AVAILABLE)
        self.assertEqual(record.count, 269)
        self.assertEqual(record.username, "huylong276")
        self.assertFalse(record.stale)

        # Verify cached
        cached = self.cache.get("huylong276")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.count, 269)

    def test_read_streak_respects_fresh_cache(self) -> None:
        mock_adb = MagicMock()
        reader = AndroidStreakReader(self.cache, adb_client=mock_adb)

        now_iso = datetime.now(timezone.utc).isoformat()
        self.cache.set(StreakRecord(
            username="alice",
            status=StreakStatus.AVAILABLE,
            count=100,
            updated_at=now_iso,
        ))

        # Should return cached without calling ADB
        record = reader.read_recipient_streak("alice", display_name="Alice")
        self.assertEqual(record.count, 100)
        mock_adb.dump_window_hierarchy.assert_not_called()

    def test_read_streak_force_refresh_bypasses_cache(self) -> None:
        mock_adb = MagicMock()
        mock_adb.dump_window_hierarchy.return_value = SAMPLE_INBOX_XML
        reader = AndroidStreakReader(self.cache, adb_client=mock_adb)

        now_iso = datetime.now(timezone.utc).isoformat()
        self.cache.set(StreakRecord(
            username="huynsiucapkhaai",
            status=StreakStatus.AVAILABLE,
            count=50,
            updated_at=now_iso,
        ))

        record = reader.read_recipient_streak(
            "huynsiucapkhaai", display_name="Én nhỏ", force_refresh=True
        )
        self.assertEqual(record.count, 71)
        mock_adb.dump_window_hierarchy.assert_called_once()

    def test_read_streak_adb_failure_marks_existing_stale(self) -> None:
        mock_adb = MagicMock()
        mock_adb.dump_window_hierarchy.side_effect = RuntimeError("ADB Connection lost")
        reader = AndroidStreakReader(self.cache, adb_client=mock_adb)

        now_iso = datetime.now(timezone.utc).isoformat()
        self.cache.set(StreakRecord(
            username="huylong276",
            status=StreakStatus.AVAILABLE,
            count=269,
            updated_at=now_iso,
        ))

        record = reader.read_recipient_streak(
            "huylong276", display_name="HuyLong", force_refresh=True
        )
        self.assertEqual(record.count, 269)
        self.assertTrue(record.stale)

    def test_read_streak_never_returns_zero_when_not_found(self) -> None:
        mock_adb = MagicMock()
        mock_adb.dump_window_hierarchy.return_value = SAMPLE_INBOX_XML
        reader = AndroidStreakReader(self.cache, adb_client=mock_adb)

        record = reader.read_recipient_streak("unknown_user", display_name="NonExistent")
        self.assertEqual(record.status, StreakStatus.NOT_VISIBLE)
        self.assertIsNone(record.count)
        self.assertNotEqual(record.count, 0)


class TestAndroidGeometricAndDeviceResolution(unittest.TestCase):
    """Test geometric bounds filtering for unread badges and dynamic device auto-detection."""

    def test_row_with_unread_badge_does_not_count_as_streak(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node index="0" text="" class="android.widget.FrameLayout">
            <node index="0" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,100][1440,350]">
              <node index="1" text="John Doe" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[200,120][450,180]" />
              <!-- Unread badge at far right edge of the screen: should NOT be parsed as streak -->
              <node index="2" text="5" resource-id="com.ss.android.ugc.trill:id/gdw" class="android.widget.TextView" bounds="[1320,120][1380,180]" content-desc="5 unread messages" />
              <node index="3" text="Hello" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" bounds="[200,190][400,240]" />
            </node>
          </node>
        </hierarchy>
        """
        items = parse_inbox_hierarchy(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].display_name, "John Doe")
        self.assertEqual(items[0].status, StreakStatus.NOT_VISIBLE)
        self.assertIsNone(items[0].count)

    def test_row_with_both_streak_and_unread_badge_picks_streak(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node index="0" text="" class="android.widget.FrameLayout">
            <node index="0" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,100][1440,350]">
              <node index="1" text="Sarah" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[200,120][450,180]" />
              <!-- Streak counter adjacent to username -->
              <node index="2" text="188" resource-id="" class="android.widget.TextView" bounds="[520,120][600,180]" />
              <!-- Unread badge at far right -->
              <node index="3" text="1" resource-id="com.ss.android.ugc.trill:id/gdw" class="android.widget.TextView" bounds="[1320,120][1380,180]" />
              <node index="4" text="Sent a video" resource-id="com.ss.android.ugc.trill:id/mxb" class="android.widget.TextView" bounds="[200,190][400,240]" />
            </node>
          </node>
        </hierarchy>
        """
        items = parse_inbox_hierarchy(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].display_name, "Sarah")
        self.assertEqual(items[0].status, StreakStatus.AVAILABLE)
        self.assertEqual(items[0].count, 188)

    def test_conflicting_numbers_in_row_returns_unknown(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node index="0" text="" class="android.widget.FrameLayout">
            <node index="0" text="" resource-id="com.ss.android.ugc.trill:id/w54" class="android.view.ViewGroup" bounds="[0,100][1440,350]">
              <node index="1" text="Confused" resource-id="com.ss.android.ugc.trill:id/user_name" class="android.widget.TextView" bounds="[200,120][450,180]" />
              <!-- Two conflicting numbers both in candidate zone -->
              <node index="2" text="42" class="android.widget.TextView" bounds="[500,120][560,180]" />
              <node index="3" text="99" class="android.widget.TextView" bounds="[570,120][630,180]" />
            </node>
          </node>
        </hierarchy>
        """
        items = parse_inbox_hierarchy(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].display_name, "Confused")
        self.assertEqual(items[0].status, StreakStatus.UNKNOWN)
        self.assertIsNone(items[0].count)

    def test_auto_detect_single_device(self) -> None:
        from daily_streak.android_streak_reader import AdbDeviceNotFoundError, AdbMultipleDevicesError

        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb", "devices"],
            returncode=0,
            stdout="List of devices attached\nLMG710ULMe527c9d1\tdevice\n\n",
            stderr="",
        ))
        client = AndroidAdbClient(device_id=None, command_runner=mock_runner)
        dev = client.resolve_device()
        self.assertEqual(dev, "LMG710ULMe527c9d1")
        self.assertEqual(client.device_id, "LMG710ULMe527c9d1")

    def test_zero_devices_raises_device_not_found(self) -> None:
        from daily_streak.android_streak_reader import AdbDeviceNotFoundError

        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb", "devices"],
            returncode=0,
            stdout="List of devices attached\n\n",
            stderr="",
        ))
        client = AndroidAdbClient(device_id=None, command_runner=mock_runner)
        with self.assertRaises(AdbDeviceNotFoundError) as ctx:
            client.resolve_device()
        self.assertIn("No Android device detected", str(ctx.exception))

    def test_multiple_devices_raises_multiple_devices_error(self) -> None:
        from daily_streak.android_streak_reader import AdbMultipleDevicesError

        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb", "devices"],
            returncode=0,
            stdout="List of devices attached\ndev1\tdevice\ndev2\tdevice\n\n",
            stderr="",
        ))
        client = AndroidAdbClient(device_id=None, command_runner=mock_runner)
        with self.assertRaises(AdbMultipleDevicesError) as ctx:
            client.resolve_device()
        self.assertIn("Multiple Android devices attached", str(ctx.exception))

    def test_explicit_device_id_verified(self) -> None:
        mock_runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=["adb", "devices"],
            returncode=0,
            stdout="List of devices attached\ndev1\tdevice\ndev2\tdevice\n\n",
            stderr="",
        ))
        client = AndroidAdbClient(device_id="dev2", command_runner=mock_runner)
        self.assertEqual(client.resolve_device(), "dev2")

    def test_adb_binary_not_found_raises_adb_unavailable(self) -> None:
        from daily_streak.android_streak_reader import AdbUnavailableError

        mock_runner = MagicMock(side_effect=FileNotFoundError("No such file or directory: 'adb'"))
        client = AndroidAdbClient(command_runner=mock_runner)
        with self.assertRaises(AdbUnavailableError) as ctx:
            client.list_devices()
        self.assertIn("not found in PATH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
