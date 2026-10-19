"""Official TikTok Android UI streak reader via UIAutomator Accessibility Tree."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence
import xml.etree.ElementTree as ET

from daily_streak.state import normalize_username
from daily_streak.streak_reader import (
    DEFAULT_CACHE_FILENAME,
    StreakCache,
    StreakRecord,
    StreakStatus,
    format_streak_display,
)

logger = logging.getLogger(__name__)

DEFAULT_ADB_TIMEOUT_SECONDS = 30.0
DEFAULT_REMOTE_DUMP_PATH = "/data/local/tmp/dump.xml"


class AdbError(RuntimeError):
    """Base exception for Android ADB operations."""


class AdbUnavailableError(AdbError):
    """Raised when ADB executable is not found or cannot be launched."""


class AdbDeviceNotFoundError(AdbError):
    """Raised when no Android device is attached or the target device is missing."""


class AdbMultipleDevicesError(AdbError):
    """Raised when multiple Android devices are connected and none was specified."""


def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """Parse bounds string formatted as '[x1,y1][x2,y2]' -> (left, top, right, bottom)."""
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str.strip())
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
    return None


def list_attached_devices(
    adb_path: str = "adb",
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout_seconds: float = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> list[str]:
    """Return list of connected device serials in 'device' state."""
    runner = command_runner or subprocess.run
    try:
        res = runner(
            [adb_path, "devices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise AdbUnavailableError(f"ADB executable '{adb_path}' not found in PATH: {exc}") from exc
    except Exception as exc:
        raise AdbUnavailableError(f"Failed to execute ADB command: {exc}") from exc

    if res.returncode != 0:
        error_msg = res.stderr.strip() or res.stdout.strip()
        raise AdbUnavailableError(f"ADB 'devices' command failed (code {res.returncode}): {error_msg}")

    devices: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("* daemon"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


@dataclass(frozen=True)
class AndroidConversationItem:
    """Individual conversation entry parsed from Android UIAutomator tree."""

    display_name: str
    status: StreakStatus
    count: int | None
    subtitle: str = ""
    bounds: str = ""


def parse_inbox_hierarchy(xml_content: str) -> list[AndroidConversationItem]:
    """Parse Android UIAutomator XML string and extract conversation streak items.

    Args:
        xml_content: The raw XML output from `uiautomator dump`.

    Returns:
        List of parsed AndroidConversationItem objects.
    """
    if not xml_content or not xml_content.strip():
        return []

    # Strip any potential leading ADB warning/info text before the XML header
    xml_start = xml_content.find("<?xml")
    if xml_start != -1:
        xml_content = xml_content[xml_start:]
    else:
        root_start = xml_content.find("<hierarchy")
        if root_start != -1:
            xml_content = xml_content[root_start:]

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        logger.warning("Failed to parse Android UIAutomator XML: %s", exc)
        return []

    results: list[AndroidConversationItem] = []

    # Iterate over nodes. Conversation rows in TikTok Android (47.x+) are ViewGroups
    # that contain a TextView with resource-id ending in ':id/user_name'.
    for node in root.iter("node"):
        res_id = node.attrib.get("resource-id", "")
        # Primary container identifier is :id/w54 or a ViewGroup housing user_name
        is_row_container = res_id.endswith(":id/w54")
        if not is_row_container:
            # Fallback: check direct children for user_name
            has_user_name_child = any(
                child.attrib.get("resource-id", "").endswith(":id/user_name")
                for child in node
            )
            if not has_user_name_child:
                continue

        user_node = None
        sub_node = None
        candidate_streak_nodes: list[tuple[ET.Element, tuple[int, int, int, int] | None]] = []

        for child in node:
            child_id = child.attrib.get("resource-id", "")
            child_text = child.attrib.get("text", "").strip()
            child_cls = child.attrib.get("class", "")
            child_desc = child.attrib.get("content-desc", "").strip()

            if child_id.endswith(":id/user_name"):
                user_node = child
            elif child_id.endswith(":id/mxb"):
                sub_node = child
            elif child_cls.endswith("TextView") and re.match(r"^\d+$", child_text):
                # Filter out obvious unread badges or notification icons
                if "unread" in child_desc.lower() or "badge" in child_id.lower() or child_id.endswith(":id/gdw"):
                    continue
                child_bounds = parse_bounds(child.attrib.get("bounds", ""))
                candidate_streak_nodes.append((child, child_bounds))

        if user_node is not None:
            raw_name = user_node.attrib.get("text", "").strip()
            # TikTok prefixes contact names with \u200e (Left-to-Right Mark)
            clean_name = raw_name.lstrip("\u200e\u200f").strip()
            if not clean_name:
                continue

            sub_text = (
                sub_node.attrib.get("text", "").strip().lstrip("\u200e\u200f").strip()
                if sub_node is not None
                else ""
            )
            bounds = node.attrib.get("bounds", "")
            row_bounds = parse_bounds(bounds)
            user_bounds = parse_bounds(user_node.attrib.get("bounds", ""))

            streak_node = None
            inconsistent_geometry = False

            if candidate_streak_nodes:
                if user_bounds is not None:
                    u_left, u_top, u_right, u_bottom = user_bounds
                    # Filter candidates geometrically:
                    # 1. Vertically overlapping or aligned with user_name
                    # 2. To the right of user_name, but not far right edge of row (unread badges)
                    matched_candidates: list[ET.Element] = []
                    for cand_el, c_b in candidate_streak_nodes:
                        if c_b is not None:
                            c_left, c_top, c_right, c_bottom = c_b
                            v_overlap = max(0, min(u_bottom, c_bottom) - max(u_top, c_top))
                            v_aligned = abs(c_top - u_top) <= 40 or v_overlap > 0
                            h_after_user = c_left >= (u_left + 20)
                            is_far_right = False
                            if row_bounds is not None:
                                is_far_right = c_right >= (row_bounds[2] - 160)
                            dist_from_user_right = c_left - u_right
                            h_adjacent = -20 <= dist_from_user_right <= 350

                            if v_aligned and h_after_user and not is_far_right and h_adjacent:
                                matched_candidates.append(cand_el)
                        else:
                            # Bounds not available on candidate
                            matched_candidates.append(cand_el)

                    if len(matched_candidates) == 1:
                        streak_node = matched_candidates[0]
                    elif len(matched_candidates) > 1:
                        # Multiple candidates: check if counts match
                        counts = set(c.attrib.get("text", "").strip() for c in matched_candidates)
                        if len(counts) == 1:
                            streak_node = matched_candidates[0]
                        else:
                            inconsistent_geometry = True
                else:
                    # No user_bounds available (e.g. synthetic test)
                    if len(candidate_streak_nodes) == 1:
                        streak_node = candidate_streak_nodes[0][0]
                    else:
                        inconsistent_geometry = True

            # Determine streak status and count
            if inconsistent_geometry:
                status = StreakStatus.UNKNOWN
                count = None
            elif streak_node is not None:
                try:
                    count = int(streak_node.attrib.get("text", "").strip())
                    if count > 0:
                        status = StreakStatus.AVAILABLE
                    else:
                        status = StreakStatus.NOT_ACTIVE
                        count = None
                except ValueError:
                    status = StreakStatus.UNKNOWN
                    count = None
            else:
                # If row is cleanly present but has no numeric streak node
                status = StreakStatus.NOT_VISIBLE
                count = None

            # Detect lost or ended indicator in subtitle
            if re.search(r"\b(?:streak lost|ended|mất chuỗi|hết hạn)\b", sub_text, re.IGNORECASE):
                status = StreakStatus.NOT_ACTIVE
                count = None

            results.append(
                AndroidConversationItem(
                    display_name=clean_name,
                    status=status,
                    count=count,
                    subtitle=sub_text,
                    bounds=bounds,
                )
            )

    return results


class AndroidAdbClient:
    """Wrapper for executing ADB commands with device auto-resolution and error handling."""

    def __init__(
        self,
        device_id: str | None = None,
        *,
        adb_path: str = "adb",
        timeout_seconds: float = DEFAULT_ADB_TIMEOUT_SECONDS,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.device_id = device_id
        self.adb_path = adb_path
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._runner = command_runner or subprocess.run

    def list_devices(self) -> list[str]:
        """Return list of connected device serials in 'device' state."""
        return list_attached_devices(
            adb_path=self.adb_path,
            command_runner=self._runner,
            timeout_seconds=self.timeout_seconds,
        )

    def resolve_device(self) -> str:
        """Resolve target device ID or auto-detect from attached devices."""
        if self.device_id:
            return self.device_id

        devices = self.list_devices()
        if not devices:
            raise AdbDeviceNotFoundError(
                "No Android device detected via ADB. "
                "Please connect your device via USB with USB Debugging enabled."
            )
        if len(devices) > 1:
            raise AdbMultipleDevicesError(
                f"Multiple Android devices attached: {devices}. "
                "Please specify a target device using --device <device_id> or in config.json."
            )
        self.device_id = devices[0]
        return self.device_id

    def _build_cmd(self, *args: str) -> list[str]:
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(["-s", self.device_id])
        cmd.extend(args)
        return cmd

    def run_cmd(self, *args: str) -> str:
        """Run an ADB command and return stdout as string."""
        cmd = self._build_cmd(*args)
        try:
            res = self._runner(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise AdbUnavailableError(f"ADB executable '{self.adb_path}' not found: {exc}") from exc
        except Exception as exc:
            logger.error("ADB command %s failed: %s", cmd, exc)
            raise AdbError(f"ADB command failed: {exc}") from exc

        if res.returncode != 0:
            error_msg = res.stderr.strip() or res.stdout.strip()
            low_msg = error_msg.lower()
            if "device not found" in low_msg or "no devices/emulators found" in low_msg:
                raise AdbDeviceNotFoundError(f"Android device not found: {error_msg}")
            raise AdbError(f"ADB exited with code {res.returncode}: {error_msg}")
        return res.stdout

    def dump_window_hierarchy(self, remote_path: str = DEFAULT_REMOTE_DUMP_PATH) -> str:
        """Dump UIAutomator hierarchy on device and return the XML string."""
        if not self.device_id:
            self.resolve_device()
        # Use compressed dump to avoid idle timeouts with video/animation surfaces
        self.run_cmd("shell", "uiautomator", "dump", "--compressed", remote_path)
        stdout = self.run_cmd("shell", "cat", remote_path)
        return stdout


class AndroidStreakReader:
    """Independent streak reader fetching streak counts directly from Android TikTok UI."""

    def __init__(
        self,
        cache: StreakCache,
        adb_client: AndroidAdbClient | None = None,
        *,
        device_id: str | None = None,
    ) -> None:
        self.cache = cache
        self.adb = adb_client or AndroidAdbClient(device_id=device_id)

    def scan_inbox_streaks(self, *, populate_cache: bool = True) -> list[AndroidConversationItem]:
        """Dump the current Android Inbox screen and return all parsed conversation items."""
        xml_content = self.adb.dump_window_hierarchy()
        items = parse_inbox_hierarchy(xml_content)
        if populate_cache:
            now_iso = datetime.now(timezone.utc).isoformat()
            for it in items:
                norm_name = normalize_username(it.display_name)
                if norm_name:
                    self.cache.set(
                        StreakRecord(
                            username=norm_name,
                            status=it.status,
                            count=it.count,
                            updated_at=now_iso,
                            stale=False,
                            raw_evidence=f"Android UIAutomator: '{it.display_name}' ({it.subtitle})",
                        )
                    )
        return items

    def read_recipient_streak(
        self,
        username: str,
        *,
        display_name: str | None = None,
        force_refresh: bool = False,
        items: Sequence[AndroidConversationItem] | None = None,
    ) -> StreakRecord:
        """Read streak for a recipient, checking cache first unless force_refresh is True.

        Args:
            username: Target recipient username (e.g. 'huynsiucapkhaai' or 'huylong276').
            display_name: Optional configured display name (e.g. 'Én nhỏ' or 'HuyLong').
            force_refresh: When True, bypass cache and read directly from Android.
            items: Optional pre-scanned list of AndroidConversationItem objects.

        Returns:
            Structured StreakRecord.
        """
        norm_username = normalize_username(username)
        now_iso = datetime.now(timezone.utc).isoformat()

        if items is None and not force_refresh:
            cached = self.cache.get(norm_username)
            if cached is not None and not cached.stale:
                return cached
            if display_name:
                cached_by_name = self.cache.get(normalize_username(display_name))
                if cached_by_name is not None and not cached_by_name.stale:
                    return cached_by_name

        if items is None:
            try:
                items = self.scan_inbox_streaks(populate_cache=True)
            except Exception as exc:
                logger.warning("Failed to scan Android Inbox: %s", exc)
                existing = self.cache.mark_stale(norm_username)
                if existing is not None:
                    return existing
                return StreakRecord(
                    username=norm_username,
                    status=StreakStatus.UNKNOWN,
                    count=None,
                    updated_at=now_iso,
                    raw_evidence=str(exc),
                )

        # Match conversation by display_name or username
        matched_item = None
        targets_to_match = []
        if display_name:
            targets_to_match.append(normalize_username(display_name))
            targets_to_match.append(display_name.strip().casefold())
        if norm_username:
            targets_to_match.append(norm_username)

        for item in items:
            item_name_norm = normalize_username(item.display_name)
            item_name_casefold = item.display_name.strip().casefold()
            if any(t == item_name_norm or t == item_name_casefold for t in targets_to_match):
                matched_item = item
                break

        if matched_item is not None:
            record = StreakRecord(
                username=norm_username,
                status=matched_item.status,
                count=matched_item.count,
                updated_at=now_iso,
                stale=False,
                raw_evidence=f"Android UIAutomator: '{matched_item.display_name}' ({matched_item.subtitle})",
            )
            self.cache.set(record)
            if display_name:
                norm_disp = normalize_username(display_name)
                if norm_disp and norm_disp != norm_username:
                    self.cache.set(
                        StreakRecord(
                            username=norm_disp,
                            status=matched_item.status,
                            count=matched_item.count,
                            updated_at=now_iso,
                            stale=False,
                            raw_evidence=f"Android UIAutomator: '{matched_item.display_name}' ({matched_item.subtitle})",
                        )
                    )
            return record

        # Not found on current visible screen
        record = StreakRecord(
            username=norm_username,
            status=StreakStatus.NOT_VISIBLE,
            count=None,
            updated_at=now_iso,
            stale=False,
            raw_evidence="Not found in current Android Inbox hierarchy",
        )
        # Note: Do not overwrite a fresh valid cache with NOT_VISIBLE if it might be off-screen
        existing = self.cache.get(norm_username)
        if existing is not None and existing.status == StreakStatus.AVAILABLE:
            existing.stale = True
            self.cache.save()
            return existing

        self.cache.set(record)
        return record
