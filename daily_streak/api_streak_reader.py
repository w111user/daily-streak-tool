"""Standalone TikTok streak reader via first-party API (GET /tiktok/v1/im/streaks/get).

Retrieves live interaction streak data directly using an authorized session cookie,
decoding the Protobuf payload in pure Python with zero phone or ADB runtime dependency.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import ssl
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from daily_streak.state import normalize_username
from daily_streak.streak_reader import (
    StreakCache,
    StreakRecord,
    StreakStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_STREAK_ENDPOINT = "https://api16-normal-c-alisg.tiktokv.com/tiktok/v1/im/streaks/get"
DEFAULT_FALLBACK_ENDPOINTS = [
    "https://api-va.tiktokv.com/tiktok/v1/im/streaks/get",
]
DEFAULT_PROFILE_ENDPOINT = "https://www.tiktok.com/tiktok/v1/im/user/profile/"
DEFAULT_PROFILE_FALLBACK_ENDPOINTS = [
    "https://api16-normal-c-alisg.tiktokv.com/tiktok/v1/im/user/profile/",
]
DEFAULT_USER_AGENT = (
    "com.ss.android.ugc.trill/470104 (Linux; U; Android 11; en_US; LMG710ULM; "
    "Build/RKQ1.210420.001; Cronet/TTNetVersion:7a79f1fa 2024-08-01 QuicVersion:f2537604 2024-04-10)"
)
DEFAULT_TIMEOUT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ApiStreakError(RuntimeError):
    """Base exception for API-based streak retrieval."""


class ApiStreakAuthError(ApiStreakError):
    """Raised when authentication credentials (cookies) are missing, invalid, or expired."""


class ApiStreakNetworkError(ApiStreakError):
    """Raised on connection timeout, network failure, or HTTP error status."""


class ApiStreakParseError(ApiStreakError):
    """Raised when the server response cannot be decoded or is malformed."""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImUserProfile:
    """Resolved IM profile from TikTok first-party user profile endpoint."""

    user_id: int
    unique_id: str
    nick_name: str = ""


@dataclass(frozen=True)
class StreakUser:
    """Individual user entry inside a streak record."""

    uid: int
    timestamp: int = 0


@dataclass
class RawStreakRecord:
    """Unmapped raw streak record decoded from the Protobuf payload."""

    conversation_id: str
    conversation_type: int
    streak_counter: int | None
    streak_timezone: str = ""
    users: list[StreakUser] = field(default_factory=list)
    active_before: int | None = None
    end_at: int | None = None
    restorable_before: int | None = None
    remain_restore_cnt: int | None = None
    streak_level: int | None = None
    peer_uid: int | None = None

    @property
    def is_group(self) -> bool:
        return self.conversation_type == 2

    @property
    def is_dm(self) -> bool:
        return self.conversation_type == 1


@dataclass
class ApiStreakResponse:
    """Complete decoded response from the streak API."""

    timestamp_millis: int
    records: list[RawStreakRecord]
    current_user_uid: int | None = None


# ---------------------------------------------------------------------------
# Pure-Python Protobuf Wire Decoder
# ---------------------------------------------------------------------------


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a base-128 varint from data starting at pos. Returns (value, new_pos)."""
    res = 0
    shift = 0
    length = len(data)
    while pos < length:
        b = data[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7
        if shift > 70:
            raise ApiStreakParseError(f"Varint overflow at position {pos}")
    raise ApiStreakParseError(f"Truncated varint at position {pos}")


def _parse_raw_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Parse raw protobuf message into list of (field_number, wire_type, raw_value)."""
    pos = 0
    length = len(data)
    fields: list[tuple[int, int, Any]] = []
    while pos < length:
        tag_wire, pos = _decode_varint(data, pos)
        wire_type = tag_wire & 7
        field_num = tag_wire >> 3
        if field_num == 0:
            raise ApiStreakParseError(f"Invalid field number 0 at position {pos}")

        if wire_type == 0:  # Varint
            val, pos = _decode_varint(data, pos)
            fields.append((field_num, 0, val))
        elif wire_type == 1:  # 64-bit
            if pos + 8 > length:
                raise ApiStreakParseError(f"Truncated fixed64 at position {pos}")
            val = data[pos : pos + 8]
            pos += 8
            fields.append((field_num, 1, val))
        elif wire_type == 2:  # Length-delimited
            flen, pos = _decode_varint(data, pos)
            if pos + flen > length:
                raise ApiStreakParseError(f"Truncated length-delimited field at position {pos} (need {flen} bytes)")
            val = data[pos : pos + flen]
            pos += flen
            fields.append((field_num, 2, val))
        elif wire_type == 5:  # 32-bit
            if pos + 4 > length:
                raise ApiStreakParseError(f"Truncated fixed32 at position {pos}")
            val = data[pos : pos + 4]
            pos += 4
            fields.append((field_num, 5, val))
        else:
            raise ApiStreakParseError(f"Unsupported wire type {wire_type} at position {pos}")
    return fields


def _decode_streak_user(data: bytes) -> StreakUser:
    """Decode an inner StreakUser protobuf message."""
    fields = _parse_raw_fields(data)
    uid = 0
    ts = 0
    for fnum, wtype, val in fields:
        if fnum == 1 and wtype == 0:
            uid = val
        elif fnum == 2 and wtype == 0:
            ts = val
    return StreakUser(uid=uid, timestamp=ts)


def _decode_streak_value(data: bytes) -> RawStreakRecord:
    """Decode a StreakValue protobuf message (tag 1 of response)."""
    fields = _parse_raw_fields(data)
    users: list[StreakUser] = []
    streak_counter: int | None = None
    streak_timezone = ""
    conv_id = ""
    conv_type = 1
    active_before: int | None = None
    end_at: int | None = None
    restorable_before: int | None = None
    remain_restore_cnt: int | None = None
    streak_level: int | None = None

    for fnum, wtype, val in fields:
        if fnum == 1 and wtype == 2:
            try:
                users.append(_decode_streak_user(val))
            except Exception as exc:
                logger.debug("Failed to decode StreakUser: %s", exc)
        elif fnum == 3 and wtype == 0:
            # Streak counter (preserve exact int, including 0)
            streak_counter = int(val)
        elif fnum == 4 and wtype == 2:
            streak_timezone = val.decode("utf-8", errors="replace")
        elif fnum == 5 and wtype == 0:
            restorable_before = val
        elif fnum == 6 and wtype == 0:
            remain_restore_cnt = val
        elif fnum == 7 and wtype == 0:
            streak_level = val
        elif fnum == 11 and wtype == 0:
            active_before = val
        elif fnum == 12 and wtype == 0:
            end_at = val
        elif fnum == 100 and wtype == 0:
            conv_type = int(val)
        elif fnum == 101 and wtype == 2:
            conv_id = val.decode("utf-8", errors="replace")

    return RawStreakRecord(
        conversation_id=conv_id,
        conversation_type=conv_type,
        streak_counter=streak_counter,
        streak_timezone=streak_timezone,
        users=users,
        active_before=active_before,
        end_at=end_at,
        restorable_before=restorable_before,
        remain_restore_cnt=remain_restore_cnt,
        streak_level=streak_level,
    )


def decode_streaks_protobuf(data: bytes) -> ApiStreakResponse:
    """Decode the top-level tiktok_v1_im_streaks_get_response Protobuf binary.

    Schema:
      tag 1: repeated StreakValue streaks
      tag 2: int64 timestamp_millis
    """
    if not data:
        raise ApiStreakParseError("Empty response body from streak endpoint")

    # Check for server error packet: { 1: 8, 2: "Login expired" }
    # Tag 1 (fnum 1, wtype 0 = varint) with value 8 and tag 2 (fnum 2, wtype 2)
    # usually indicates Bd-Tt-Error-Code: 8
    try:
        fields = _parse_raw_fields(data)
    except Exception as exc:
        raise ApiStreakParseError(f"Protobuf wire decoding error: {exc}") from exc

    # Check if this is an error payload
    # When Bd-Tt-Error-Code != 0, tag 1 is varint error_code and tag 2 is string error_msg
    is_error_payload = any(fnum == 1 and wtype == 0 for fnum, wtype, _ in fields)
    if is_error_payload:
        err_code = next((val for fnum, wtype, val in fields if fnum == 1 and wtype == 0), 0)
        err_msg = ""
        for fnum, wtype, val in fields:
            if fnum == 2 and wtype == 2:
                err_msg = val.decode("utf-8", errors="replace")
        if err_code == 8 or "login expired" in err_msg.lower():
            raise ApiStreakAuthError(f"TikTok session expired (error {err_code}: {err_msg})")
        raise ApiStreakError(f"TikTok streak API error (code {err_code}): {err_msg}")

    records: list[RawStreakRecord] = []
    timestamp_millis = 0

    for fnum, wtype, val in fields:
        if fnum == 1 and wtype == 2:
            records.append(_decode_streak_value(val))
        elif fnum == 2 and wtype == 0:
            timestamp_millis = int(val)

    # Infer current user's UID across 1-on-1 conversations
    current_user_uid = _infer_current_user_uid(records)
    if current_user_uid is not None:
        for r in records:
            if r.is_dm and len(r.users) >= 2:
                for u in r.users:
                    if u.uid != current_user_uid:
                        r.peer_uid = u.uid
                        break
            elif r.is_dm and r.conversation_id.startswith("0:1:"):
                # Parse 0:1:<uid1>:<uid2>
                parts = r.conversation_id.split(":")
                if len(parts) >= 4:
                    try:
                        u1 = int(parts[2])
                        u2 = int(parts[3])
                        r.peer_uid = u1 if u2 == current_user_uid else u2
                    except (ValueError, IndexError):
                        pass

    return ApiStreakResponse(
        timestamp_millis=timestamp_millis,
        records=records,
        current_user_uid=current_user_uid,
    )


def _infer_current_user_uid(records: Sequence[RawStreakRecord]) -> int | None:
    """Find the account UID common to all 1-on-1 DM conversations."""
    dm_records = [r for r in records if r.is_dm and len(r.users) >= 2]
    if not dm_records:
        # Fall back to inspecting conversation_id: "0:1:<uid1>:<uid2>"
        parsed_uids: list[set[int]] = []
        for r in records:
            if r.is_dm and r.conversation_id.startswith("0:1:"):
                parts = r.conversation_id.split(":")
                if len(parts) >= 4:
                    try:
                        parsed_uids.append({int(parts[2]), int(parts[3])})
                    except (ValueError, IndexError):
                        pass
        if parsed_uids:
            common = set.intersection(*parsed_uids)
            if len(common) == 1:
                return next(iter(common))
        return None

    # Find the UID present in all DM records
    user_sets = [{u.uid for u in r.users} for r in dm_records]
    common = set.intersection(*user_sets)
    if len(common) == 1:
        return next(iter(common))
    if len(common) > 1:
        # If multiple common, pick the one with highest frequency
        counts: Counter[int] = Counter()
        for s in user_sets:
            counts.update(s)
        most_common = counts.most_common(1)
        if most_common:
            return most_common[0][0]
    return None


# ---------------------------------------------------------------------------
# Cookie Handling
# ---------------------------------------------------------------------------


def load_cookies_from_file(cookie_file: Path | str) -> str:
    """Read a JSON cookie file and construct an HTTP Cookie header string.

    Never logs cookie values. Validates that the file exists and contains a session.
    """
    path = Path(cookie_file)
    if not path.is_file():
        raise ApiStreakAuthError(f"Cookie file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        cookies = json.loads(content)
    except Exception as exc:
        raise ApiStreakAuthError(f"Failed to read or parse cookie file {path}: {exc}") from exc

    if not isinstance(cookies, list) or not cookies:
        raise ApiStreakAuthError(f"Cookie file {path} contains no cookie list")

    parts: list[str] = []
    has_session = False
    for c in cookies:
        if isinstance(c, Mapping):
            name = str(c.get("name") or "").strip()
            val = str(c.get("value") or "").strip()
            if name and val:
                parts.append(f"{name}={val}")
                if name in ("sessionid", "sessionid_ss"):
                    has_session = True

    if not parts:
        raise ApiStreakAuthError(f"No valid name=value pairs found in {path}")
    if not has_session:
        logger.warning("No 'sessionid' cookie found in %s; request may fail authentication", path)

    return "; ".join(parts)


# ---------------------------------------------------------------------------
# ApiStreakReader
# ---------------------------------------------------------------------------


class ApiStreakReader:
    """Standalone client to read live TikTok streak counts via the verified HTTP endpoint."""

    def __init__(
        self,
        cache: StreakCache,
        *,
        cookie_file: Path | str | None = None,
        cookie_header: str | None = None,
        endpoint_url: str = DEFAULT_STREAK_ENDPOINT,
        fallback_endpoints: Sequence[str] | None = None,
        profile_endpoint_url: str = DEFAULT_PROFILE_ENDPOINT,
        profile_fallback_endpoints: Sequence[str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        http_client: Callable[..., Any] | None = None,
    ) -> None:
        self.cache = cache
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self._cookie_header = cookie_header
        self.endpoint_url = endpoint_url
        self.fallback_endpoints = list(fallback_endpoints) if fallback_endpoints is not None else DEFAULT_FALLBACK_ENDPOINTS
        self.profile_endpoint_url = profile_endpoint_url
        self.profile_fallback_endpoints = (
            list(profile_fallback_endpoints) if profile_fallback_endpoints is not None else DEFAULT_PROFILE_FALLBACK_ENDPOINTS
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.user_agent = user_agent
        self._http_client = http_client
        self._resolved_profiles: dict[int, ImUserProfile] = {}

    def get_cookie_header(self) -> str:
        """Resolve the Cookie header string, loading from file if necessary."""
        if self._cookie_header:
            return self._cookie_header
        if self.cookie_file:
            return load_cookies_from_file(self.cookie_file)
        raise ApiStreakAuthError("No cookie file or cookie header configured for ApiStreakReader")

    def fetch_all_streaks(self) -> ApiStreakResponse:
        """Query the first-party TikTok streaks endpoint and decode the response."""
        cookie_hdr = self.get_cookie_header()
        endpoints = [self.endpoint_url] + [ep for ep in self.fallback_endpoints if ep != self.endpoint_url]
        last_error: Exception | None = None

        for endpoint in endpoints:
            try:
                return self._fetch_endpoint(endpoint, cookie_hdr)
            except ApiStreakAuthError:
                # Auth errors are fatal across all regional endpoints; do not retry other endpoints
                raise
            except Exception as exc:
                last_error = exc
                logger.debug("Streak endpoint %s failed: %s; trying fallback", endpoint, exc)

        if last_error is not None:
            if isinstance(last_error, ApiStreakError):
                raise last_error
            raise ApiStreakNetworkError(f"Failed to fetch streaks from all endpoints: {last_error}") from last_error
        raise ApiStreakNetworkError("No streak endpoints available to query")

    def _fetch_endpoint(self, url: str, cookie_header: str) -> ApiStreakResponse:
        """Perform a single HTTPS request to the given endpoint."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/x-protobuf",
            "Cookie": cookie_header,
        }

        if self._http_client is not None:
            status_code, resp_headers, body = self._http_client(url, headers=headers, timeout=self.timeout_seconds)
        else:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=self.timeout_seconds) as resp:
                    status_code = resp.status
                    resp_headers = dict(resp.headers)
                    body = resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ApiStreakAuthError(f"Authentication failed (HTTP {exc.code}) on {url}") from exc
                if exc.code == 429:
                    raise ApiStreakNetworkError(f"TikTok rate limit reached (HTTP 429) on {url}") from exc
                raise ApiStreakNetworkError(f"HTTP error {exc.code} on {url}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                raise ApiStreakNetworkError(f"Network error accessing {url}: {exc}") from exc
            except Exception as exc:
                raise ApiStreakNetworkError(f"Unexpected error requesting {url}: {exc}") from exc

        # Check for TikTok error header
        bd_err = resp_headers.get("Bd-Tt-Error-Code") or resp_headers.get("bd-tt-error-code")
        if bd_err and str(bd_err).strip() not in ("0", ""):
            err_code_int = int(bd_err) if str(bd_err).isdigit() else -1
            if err_code_int == 8:
                raise ApiStreakAuthError("TikTok session expired (Bd-Tt-Error-Code: 8)")
            logger.warning("TikTok streak response carried Bd-Tt-Error-Code: %s", bd_err)

        if status_code != 200:
            raise ApiStreakNetworkError(f"Unexpected HTTP status {status_code} from {url}")

        return decode_streaks_protobuf(body)

    def resolve_uids_to_profiles(
        self,
        uids: Sequence[int],
        *,
        force: bool = False,
    ) -> dict[int, ImUserProfile]:
        """Resolve TikTok numeric user IDs to their IM profile (username & nickname).

        Queries the first-party batch endpoint:
          GET /tiktok/v1/im/user/profile/?aid=1988&user_ids=[<uid1>,<uid2>,...]
        """
        valid_uids: list[int] = []
        for u in uids:
            try:
                val = int(u)
                if val > 0 and val not in valid_uids:
                    valid_uids.append(val)
            except (ValueError, TypeError):
                continue

        if not valid_uids:
            return {}

        needed_uids = [u for u in valid_uids if force or u not in self._resolved_profiles]
        if needed_uids:
            chunk_size = 50
            for i in range(0, len(needed_uids), chunk_size):
                chunk = needed_uids[i : i + chunk_size]
                try:
                    self._fetch_profile_batch(chunk)
                except Exception as exc:
                    logger.warning("Failed to resolve profiles for UIDs %s: %s", chunk, exc)

        return {u: self._resolved_profiles[u] for u in valid_uids if u in self._resolved_profiles}

    def _fetch_profile_batch(self, uids: Sequence[int]) -> None:
        """Query profile endpoint for a single batch of UIDs."""
        cookie_hdr = self.get_cookie_header()
        endpoints = [self.profile_endpoint_url] + [
            ep for ep in self.profile_fallback_endpoints if ep != self.profile_endpoint_url
        ]
        encoded_uids = urllib.parse.quote(json.dumps(list(uids)))
        last_error: Exception | None = None

        for endpoint in endpoints:
            url = f"{endpoint}?aid=1988&user_ids={encoded_uids}"
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Cookie": cookie_hdr,
                "Referer": "https://www.tiktok.com/messages",
            }
            try:
                if self._http_client is not None:
                    status_code, resp_headers, body = self._http_client(
                        url, headers=headers, timeout=self.timeout_seconds
                    )
                else:
                    ctx = ssl.create_default_context()
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, context=ctx, timeout=self.timeout_seconds) as resp:
                        status_code = resp.status
                        resp_headers = dict(resp.headers)
                        body = resp.read()

                if status_code != 200:
                    raise ApiStreakNetworkError(f"HTTP {status_code} from profile endpoint {endpoint}")

                data = json.loads(body.decode("utf-8", errors="replace"))
                users = data.get("users") or []
                for u in users:
                    prof = u.get("im_user_profile") or {}
                    raw_uid = prof.get("user_id") or prof.get("user_id_str")
                    if raw_uid is not None:
                        try:
                            uid_int = int(raw_uid)
                        except (ValueError, TypeError):
                            continue
                        unique_id = str(prof.get("unique_id") or "").strip()
                        nick_name = str(prof.get("nick_name") or "").strip()
                        self._resolved_profiles[uid_int] = ImUserProfile(
                            user_id=uid_int,
                            unique_id=unique_id,
                            nick_name=nick_name,
                        )
                return
            except ApiStreakAuthError:
                raise
            except Exception as exc:
                last_error = exc
                logger.debug("Profile endpoint %s failed: %s; trying fallback", endpoint, exc)

        if last_error is not None:
            logger.warning("Failed to resolve profiles from all endpoints: %s", last_error)

    def match_recipient_streak(
        self,
        recipient: Mapping[str, Any],
        response: ApiStreakResponse,
        *,
        resolved_uids: Mapping[str, int] | None = None,
    ) -> StreakRecord:
        """Map decoded streak records to a specific configured recipient.

        Never guesses based on display name alone.
        Matches by:
          1. Explicit conversation_id in recipient config or cache.
          2. Explicit user_id / uid in recipient config, resolved profiles, or cache.
        Preserves valid 0 counts without treating them as absent.
        """
        raw_username = str(recipient.get("username") or "").strip()
        norm_username = normalize_username(raw_username)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Check if recipient specifies conversation_id or user_id
        target_conv_id = str(recipient.get("conversation_id") or "").strip()
        target_uid: int | None = None
        raw_uid = recipient.get("user_id") or recipient.get("uid")
        if raw_uid is not None:
            try:
                target_uid = int(raw_uid)
            except (ValueError, TypeError):
                pass

        # If UID not in recipient, check resolved_uids (username -> user_id)
        if target_uid is None and norm_username:
            if resolved_uids and norm_username in resolved_uids:
                target_uid = resolved_uids[norm_username]
            else:
                for p in self._resolved_profiles.values():
                    if p.unique_id and normalize_username(p.unique_id) == norm_username:
                        target_uid = p.user_id
                        break

        # If not in recipient dict or resolved, check if existing cache record has them
        if (not target_conv_id or target_uid is None) and norm_username and self.cache is not None:
            cached = self.cache.get(norm_username)
            if cached is not None:
                if not target_conv_id and cached.conversation_id:
                    target_conv_id = cached.conversation_id
                if target_uid is None and cached.user_id is not None:
                    target_uid = cached.user_id

        # Search matching record
        matched_rec: RawStreakRecord | None = None

        if target_conv_id:
            for r in response.records:
                if r.conversation_id == target_conv_id:
                    matched_rec = r
                    break

        if matched_rec is None and target_uid is not None:
            for r in response.records:
                if r.peer_uid == target_uid:
                    matched_rec = r
                    break
                if any(u.uid == target_uid for u in r.users):
                    matched_rec = r
                    break
                # Check conversation_id format: "0:1:<uid1>:<uid2>"
                if str(target_uid) in r.conversation_id.split(":"):
                    matched_rec = r
                    break

        if matched_rec is None:
            # Unmatched / Ambiguous state
            return StreakRecord(
                username=norm_username or raw_username,
                status=StreakStatus.UNKNOWN,
                count=None,
                updated_at=now_iso,
                stale=False,
                raw_evidence="No matching conversation or UID found in streak API response",
                conversation_id=target_conv_id or None,
                user_id=target_uid,
            )

        # Record matched
        peer_uid = matched_rec.peer_uid or target_uid
        conv_id = matched_rec.conversation_id or target_conv_id

        if matched_rec.streak_counter is not None:
            return StreakRecord(
                username=norm_username or raw_username,
                status=StreakStatus.AVAILABLE,
                count=matched_rec.streak_counter,
                updated_at=now_iso,
                stale=False,
                raw_evidence=f"API verified ({matched_rec.streak_timezone or 'UTC'})",
                conversation_id=conv_id or None,
                user_id=peer_uid,
                timezone=matched_rec.streak_timezone or None,
                active_before=matched_rec.active_before,
                end_at=matched_rec.end_at,
            )

        return StreakRecord(
            username=norm_username or raw_username,
            status=StreakStatus.NOT_ACTIVE,
            count=None,
            updated_at=now_iso,
            stale=False,
            raw_evidence="Conversation has no active streak",
            conversation_id=conv_id or None,
            user_id=peer_uid,
            timezone=matched_rec.streak_timezone or None,
            active_before=matched_rec.active_before,
            end_at=matched_rec.end_at,
        )

    def refresh_recipients(
        self,
        recipients: Sequence[Mapping[str, Any]],
        *,
        force_refresh: bool = False,
        populate_cache: bool = True,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[StreakRecord]:
        """Fetch fresh streaks from TikTok and map to all requested recipients.

        Updates the durable StreakCache and returns fresh StreakRecords.
        """
        if not recipients:
            return []

        # If not forcing, check if all requested recipients already have fresh cache entries
        if not force_refresh and self.cache is not None:
            all_fresh = True
            cached_results: list[StreakRecord] = []
            for r in recipients:
                un = normalize_username(str(r.get("username") or ""))
                rec = self.cache.get(un) if un else None
                if rec is None or rec.stale:
                    all_fresh = False
                    break
                cached_results.append(rec)
            if all_fresh:
                return cached_results

        # Call live API
        api_response = self.fetch_all_streaks()

        # Collect peer UIDs from streak records and resolve them in batch
        peer_uids = [r.peer_uid for r in api_response.records if r.peer_uid is not None]
        for r in recipients:
            raw_uid = r.get("user_id") or r.get("uid")
            if raw_uid is not None:
                try:
                    peer_uids.append(int(raw_uid))
                except (ValueError, TypeError):
                    pass
            un = normalize_username(str(r.get("username") or ""))
            if un and self.cache is not None:
                cached = self.cache.get(un)
                if cached and cached.user_id is not None:
                    peer_uids.append(cached.user_id)

        self.resolve_uids_to_profiles(peer_uids, force=force_refresh)
        username_to_uid = {
            normalize_username(p.unique_id): p.user_id
            for p in self._resolved_profiles.values()
            if p.unique_id
        }

        results: list[StreakRecord] = []
        total = len(recipients)
        for idx, r in enumerate(recipients, start=1):
            record = self.match_recipient_streak(r, api_response, resolved_uids=username_to_uid)
            if self.cache is not None:
                if populate_cache and record.status != StreakStatus.UNKNOWN and record.username:
                    self.cache.set(record)
                elif record.username and record.status == StreakStatus.UNKNOWN:
                    # If unknown, keep existing cached entry but mark as stale
                    if hasattr(self.cache, "mark_stale"):
                        existing = self.cache.mark_stale(record.username)
                    else:
                        existing = self.cache.get(record.username)
                        if existing is not None:
                            existing.stale = True
                            if hasattr(self.cache, "save"):
                                self.cache.save()
                    if existing is not None:
                        record = existing
            results.append(record)
            if on_progress:
                on_progress(idx, total)

        return results

    def read_recipient_streak(
        self,
        username: str,
        *,
        display_name: str | None = None,
        force_refresh: bool = False,
        **kwargs: Any,
    ) -> StreakRecord:
        """Read streak for a single recipient, checking cache or querying live API."""
        norm_username = normalize_username(username)
        if not force_refresh and norm_username and self.cache is not None:
            cached = self.cache.get(norm_username)
            if cached is not None and not cached.stale:
                return cached

        resp = self.fetch_all_streaks()
        peer_uids = [r.peer_uid for r in resp.records if r.peer_uid is not None]
        self.resolve_uids_to_profiles(peer_uids, force=force_refresh)
        username_to_uid = {
            normalize_username(p.unique_id): p.user_id
            for p in self._resolved_profiles.values()
            if p.unique_id
        }
        record = self.match_recipient_streak(
            {"username": norm_username or username, "name": display_name},
            resp,
            resolved_uids=username_to_uid,
        )
        if self.cache is not None:
            if record.status != StreakStatus.UNKNOWN and record.username:
                self.cache.set(record)
            elif record.username and record.status == StreakStatus.UNKNOWN:
                if hasattr(self.cache, "mark_stale"):
                    existing = self.cache.mark_stale(record.username)
                else:
                    existing = self.cache.get(record.username)
                    if existing is not None:
                        existing.stale = True
                        if hasattr(self.cache, "save"):
                            self.cache.save()
                if existing is not None:
                    return existing
        return record
