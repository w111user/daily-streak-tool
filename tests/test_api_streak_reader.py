"""Comprehensive unit tests for ApiStreakReader and standalone Protobuf streak decoding."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from daily_streak.api_streak_reader import (
    ApiStreakAuthError,
    ApiStreakError,
    ApiStreakNetworkError,
    ApiStreakParseError,
    ApiStreakReader,
    ImUserProfile,
    RawStreakRecord,
    StreakUser,
    _decode_varint,
    _infer_current_user_uid,
    _parse_raw_fields,
    decode_streaks_protobuf,
    load_cookies_from_file,
)
from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus


def _encode_varint(val: int) -> bytes:
    buf = bytearray()
    while val >= 0x80:
        buf.append((val & 0x7F) | 0x80)
        val >>= 7
    buf.append(val & 0x7F)
    return bytes(buf)


def _encode_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _encode_length_delimited(field_num: int, data: bytes) -> bytes:
    return _encode_tag(field_num, 2) + _encode_varint(len(data)) + data


def _encode_varint_field(field_num: int, val: int) -> bytes:
    return _encode_tag(field_num, 0) + _encode_varint(val)


def _build_streak_user(uid: int, ts: int = 123456789) -> bytes:
    return _encode_varint_field(1, uid) + _encode_varint_field(2, ts)


def _build_streak_value(
    *,
    users: list[int] | None = None,
    streak_counter: int | None = None,
    streak_timezone: str = "Asia/Ho_Chi_Minh",
    conv_id: str = "0:1:1111:2222",
    conv_type: int = 1,
    active_before: int | None = 1700000000,
    end_at: int | None = 1700100000,
) -> bytes:
    body = bytearray()
    if users:
        for u in users:
            body.extend(_encode_length_delimited(1, _build_streak_user(u)))
    if streak_counter is not None:
        body.extend(_encode_varint_field(3, streak_counter))
    if streak_timezone:
        body.extend(_encode_length_delimited(4, streak_timezone.encode("utf-8")))
    if active_before is not None:
        body.extend(_encode_varint_field(11, active_before))
    if end_at is not None:
        body.extend(_encode_varint_field(12, end_at))
    body.extend(_encode_varint_field(100, conv_type))
    body.extend(_encode_length_delimited(101, conv_id.encode("utf-8")))
    return bytes(body)


def _build_response(records: list[bytes], ts_millis: int = 1729000000000) -> bytes:
    body = bytearray()
    for rec in records:
        body.extend(_encode_length_delimited(1, rec))
    body.extend(_encode_varint_field(2, ts_millis))
    return bytes(body)


class TestProtobufWireDecoder(unittest.TestCase):
    """Test the low-level base-128 varint and field parser."""

    def test_decode_varint_single_and_multibyte(self) -> None:
        # 1 byte
        val, pos = _decode_varint(b"\x00", 0)
        self.assertEqual((val, pos), (0, 1))

        val, pos = _decode_varint(b"\x7F", 0)
        self.assertEqual((val, pos), (127, 1))

        # 2 bytes: 300 = 0x12c -> 0xAC 0x02
        val, pos = _decode_varint(b"\xac\x02", 0)
        self.assertEqual((val, pos), (300, 2))

        # 64-bit int
        big_val = 7123456789012345678
        encoded = _encode_varint(big_val)
        val, pos = _decode_varint(encoded, 0)
        self.assertEqual(val, big_val)
        self.assertEqual(pos, len(encoded))

    def test_decode_varint_truncated_and_overflow(self) -> None:
        with self.assertRaises(ApiStreakParseError) as ctx:
            _decode_varint(b"\x80", 0)
        self.assertIn("Truncated", str(ctx.exception))

        overflow_bytes = b"\x80" * 12
        with self.assertRaises(ApiStreakParseError) as ctx:
            _decode_varint(overflow_bytes, 0)
        self.assertIn("overflow", str(ctx.exception).lower())

    def test_parse_raw_fields_types(self) -> None:
        # Varint (type 0), length-delimited (type 2), fixed32 (type 5), fixed64 (type 1)
        tag0 = _encode_tag(1, 0) + _encode_varint(42)
        tag2 = _encode_tag(2, 2) + _encode_varint(4) + b"test"
        tag5 = _encode_tag(3, 5) + b"\x01\x02\x03\x04"
        tag1 = _encode_tag(4, 1) + b"\x01\x02\x03\x04\x05\x06\x07\x08"

        payload = tag0 + tag2 + tag5 + tag1
        fields = _parse_raw_fields(payload)
        self.assertEqual(len(fields), 4)
        self.assertEqual(fields[0], (1, 0, 42))
        self.assertEqual(fields[1], (2, 2, b"test"))
        self.assertEqual(fields[2], (3, 5, b"\x01\x02\x03\x04"))
        self.assertEqual(fields[3], (4, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"))

    def test_parse_raw_fields_invalid_tag(self) -> None:
        with self.assertRaises(ApiStreakParseError):
            _parse_raw_fields(b"\x00")  # field number 0 is invalid in protobuf


class TestProtobufStreaksDecoder(unittest.TestCase):
    """Test top-level response decoding, error packets, and field extraction."""

    def test_empty_payload_raises(self) -> None:
        with self.assertRaises(ApiStreakParseError):
            decode_streaks_protobuf(b"")

    def test_login_expired_error_packet(self) -> None:
        # Server error packet: { 1: 8, 2: "Login expired" }
        payload = _encode_varint_field(1, 8) + _encode_length_delimited(2, b"Login expired")
        with self.assertRaises(ApiStreakAuthError) as ctx:
            decode_streaks_protobuf(payload)
        self.assertIn("session expired", str(ctx.exception).lower())

    def test_other_error_packet(self) -> None:
        payload = _encode_varint_field(1, 1001) + _encode_length_delimited(2, b"Internal server error")
        with self.assertRaises(ApiStreakError) as ctx:
            decode_streaks_protobuf(payload)
        self.assertIn("1001", str(ctx.exception))

    def test_decode_valid_records(self) -> None:
        rec1 = _build_streak_value(
            users=[1001, 2002],
            streak_counter=71,
            streak_timezone="Asia/Ho_Chi_Minh",
            conv_id="0:1:1001:2002",
            conv_type=1,
            active_before=1729001000,
            end_at=1729002000,
        )
        rec2 = _build_streak_value(
            users=[1001, 3003],
            streak_counter=0,  # Valid zero streak count
            streak_timezone="Asia/Bangkok",
            conv_id="0:1:1001:3003",
            conv_type=1,
        )
        rec_group = _build_streak_value(
            users=[1001, 2002, 3003],
            streak_counter=15,
            conv_id="1234567890",
            conv_type=2,
        )

        payload = _build_response([rec1, rec2, rec_group], ts_millis=1729111222333)
        resp = decode_streaks_protobuf(payload)

        self.assertEqual(resp.timestamp_millis, 1729111222333)
        self.assertEqual(len(resp.records), 3)
        self.assertEqual(resp.current_user_uid, 1001)

        r1 = resp.records[0]
        self.assertEqual(r1.streak_counter, 71)
        self.assertEqual(r1.peer_uid, 2002)
        self.assertEqual(r1.streak_timezone, "Asia/Ho_Chi_Minh")
        self.assertTrue(r1.is_dm)

        r2 = resp.records[1]
        self.assertEqual(r2.streak_counter, 0)  # Preserved as 0
        self.assertEqual(r2.peer_uid, 3003)
        self.assertTrue(r2.is_dm)

        r3 = resp.records[2]
        self.assertEqual(r3.streak_counter, 15)
        self.assertTrue(r3.is_group)

    def test_real_fixture_file_decoding(self) -> None:
        fixture_path = Path("/tmp/test_streaks_sample.pb")
        if not fixture_path.is_file():
            self.skipTest("Sample fixture not found")
        data = fixture_path.read_bytes()
        resp = decode_streaks_protobuf(data)
        self.assertGreater(len(resp.records), 0)
        self.assertIsNotNone(resp.current_user_uid)
        # Check that huylong276 count 269 is present in fixture
        has_269 = any(r.streak_counter == 269 for r in resp.records)
        self.assertTrue(has_269)


class TestCookieHandling(unittest.TestCase):
    """Test loading cookies from file with confidentiality guarantees."""

    def test_missing_file_raises_auth_error(self) -> None:
        with self.assertRaises(ApiStreakAuthError):
            load_cookies_from_file("/tmp/non_existent_cookie_file.json")

    def test_malformed_json_raises_auth_error(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            cookie_path = Path(tmp_dir) / "bad_cookies.json"
            cookie_path.write_text("NOT_JSON", encoding="utf-8")
            with self.assertRaises(ApiStreakAuthError):
                load_cookies_from_file(cookie_path)

    def test_empty_cookie_list_raises_auth_error(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            cookie_path = Path(tmp_dir) / "empty.json"
            cookie_path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ApiStreakAuthError):
                load_cookies_from_file(cookie_path)

    def test_valid_cookies_parsed(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            cookie_path = Path(tmp_dir) / "cookies.json"
            cookie_path.write_text(
                json.dumps([
                    {"name": "sessionid", "value": "secret_sess_123"},
                    {"name": "tt_chain_token", "value": "token_abc"},
                ]),
                encoding="utf-8",
            )
            header = load_cookies_from_file(cookie_path)
            self.assertIn("sessionid=secret_sess_123", header)
            self.assertIn("tt_chain_token=token_abc", header)


class TestApiStreakReader(unittest.TestCase):
    """Test ApiStreakReader logic, network error handling, and recipient matching."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.cache = StreakCache(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_match_recipient_by_conversation_id(self) -> None:
        reader = ApiStreakReader(self.cache, cookie_header="sessionid=fake")
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=100,
            conv_id="0:1:1001:2002",
        )
        resp = decode_streaks_protobuf(_build_response([rec]))

        recipient = {"name": "Bob", "username": "bob_user", "conversation_id": "0:1:1001:2002"}
        match = reader.match_recipient_streak(recipient, resp)
        self.assertEqual(match.status, StreakStatus.AVAILABLE)
        self.assertEqual(match.count, 100)
        self.assertEqual(match.username, "bob_user")
        self.assertEqual(match.conversation_id, "0:1:1001:2002")

    def test_match_recipient_by_user_id(self) -> None:
        reader = ApiStreakReader(self.cache, cookie_header="sessionid=fake")
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=55,
            conv_id="0:1:1001:2002",
        )
        resp = decode_streaks_protobuf(_build_response([rec]))

        recipient = {"name": "Charlie", "username": "charlie", "user_id": 2002}
        match = reader.match_recipient_streak(recipient, resp)
        self.assertEqual(match.status, StreakStatus.AVAILABLE)
        self.assertEqual(match.count, 55)
        self.assertEqual(match.user_id, 2002)

    def test_match_recipient_unknown_when_not_in_response(self) -> None:
        reader = ApiStreakReader(self.cache, cookie_header="sessionid=fake")
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=10,
            conv_id="0:1:1001:2002",
        )
        resp = decode_streaks_protobuf(_build_response([rec]))

        recipient = {"name": "Unknown Person", "username": "stranger"}
        match = reader.match_recipient_streak(recipient, resp)
        self.assertEqual(match.status, StreakStatus.UNKNOWN)
        self.assertIsNone(match.count)

    def test_match_recipient_with_zero_count(self) -> None:
        reader = ApiStreakReader(self.cache, cookie_header="sessionid=fake")
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=0,  # Zero count
            conv_id="0:1:1001:2002",
        )
        resp = decode_streaks_protobuf(_build_response([rec]))

        recipient = {"name": "Zero User", "username": "zero_user", "user_id": 2002}
        match = reader.match_recipient_streak(recipient, resp)
        self.assertEqual(match.status, StreakStatus.AVAILABLE)
        self.assertEqual(match.count, 0)

    def test_match_recipient_not_active_streak(self) -> None:
        reader = ApiStreakReader(self.cache, cookie_header="sessionid=fake")
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=None,  # No streak
            conv_id="0:1:1001:2002",
        )
        resp = decode_streaks_protobuf(_build_response([rec]))

        recipient = {"name": "Inactive User", "username": "inactive", "user_id": 2002}
        match = reader.match_recipient_streak(recipient, resp)
        self.assertEqual(match.status, StreakStatus.NOT_ACTIVE)
        self.assertIsNone(match.count)

    def test_fetch_endpoint_fallback_on_network_error(self) -> None:
        calls: list[str] = []

        def fake_http(url: str, **kwargs):
            calls.append(url)
            if "primary" in url:
                raise ConnectionResetError("Connection dropped")
            rec = _build_streak_value(users=[1001, 2002], streak_counter=42, conv_id="0:1:1001:2002")
            body = _build_response([rec])
            return 200, {"Bd-Tt-Error-Code": "0"}, body

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=fake",
            endpoint_url="https://primary.example.com",
            fallback_endpoints=["https://fallback.example.com"],
            http_client=fake_http,
        )
        resp = reader.fetch_all_streaks()
        self.assertEqual(len(calls), 2)
        self.assertIn("primary", calls[0])
        self.assertIn("fallback", calls[1])
        self.assertEqual(len(resp.records), 1)
        self.assertEqual(resp.records[0].streak_counter, 42)

    def test_fetch_endpoint_auth_error_does_not_retry_fallbacks(self) -> None:
        calls: list[str] = []

        def fake_http(url: str, **kwargs):
            calls.append(url)
            # Simulate session expired
            return 200, {"Bd-Tt-Error-Code": "8"}, b"\x08\x08"

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=expired",
            endpoint_url="https://primary.example.com",
            fallback_endpoints=["https://fallback.example.com"],
            http_client=fake_http,
        )
        with self.assertRaises(ApiStreakAuthError):
            reader.fetch_all_streaks()
        self.assertEqual(len(calls), 1)  # Stopped immediately without retrying

    def test_refresh_recipients_updates_cache_and_reports_progress(self) -> None:
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=88,
            conv_id="0:1:1001:2002",
        )
        body = _build_response([rec])

        def fake_http(url: str, **kwargs):
            return 200, {}, body

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        progress_calls: list[tuple[int, int]] = []
        recipients = [
            {"name": "Alice", "username": "alice", "user_id": 2002},
            {"name": "Bob", "username": "bob", "user_id": 9999},
        ]

        results = reader.refresh_recipients(
            recipients,
            force_refresh=True,
            on_progress=lambda cur, tot: progress_calls.append((cur, tot)),
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, StreakStatus.AVAILABLE)
        self.assertEqual(results[0].count, 88)
        self.assertEqual(results[1].status, StreakStatus.UNKNOWN)

        # Check cache was populated for alice
        cached_alice = self.cache.get("alice")
        self.assertIsNotNone(cached_alice)
        self.assertEqual(cached_alice.count, 88)

        # Check progress called for each recipient
        self.assertEqual(progress_calls, [(1, 2), (2, 2)])

    def test_read_recipient_streak_single(self) -> None:
        rec = _build_streak_value(
            users=[1001, 2002],
            streak_counter=33,
            conv_id="0:1:1001:2002",
        )
        body = _build_response([rec])

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=lambda url, **kw: (200, {}, body),
        )

        # First call hits API
        r1 = reader.read_recipient_streak("target_user", force_refresh=True)
        # Without matching UID or conv_id it is unknown
        self.assertEqual(r1.status, StreakStatus.UNKNOWN)

        # Prepopulate cache with conversation_id
        self.cache.set(
            StreakRecord(
                username="target_user",
                status=StreakStatus.AVAILABLE,
                count=30,
                conversation_id="0:1:1001:2002",
            )
        )
        # Calling with force_refresh=True will resolve via cached conversation_id
        r2 = reader.read_recipient_streak("target_user", force_refresh=True)
        self.assertEqual(r2.status, StreakStatus.AVAILABLE)
        self.assertEqual(r2.count, 33)


class TestUserIdResolution(unittest.TestCase):
    """Test first-party batch user profile resolution and recipient UID matching."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.cache = StreakCache(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_uids_to_profiles_success(self) -> None:
        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {
                    "im_user_profile": {
                        "user_id": 1001,
                        "unique_id": "alice_cool",
                        "nick_name": "Alice in Wonderland",
                    }
                },
                {
                    "im_user_profile": {
                        "user_id": 2002,
                        "unique_id": "bob_builder",
                        "nick_name": "Bob The Builder",
                    }
                },
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            self.assertIn("user_ids=", url)
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=fake",
            http_client=fake_http,
        )
        profiles = reader.resolve_uids_to_profiles([1001, 2002])
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[1001].unique_id, "alice_cool")
        self.assertEqual(profiles[1001].nick_name, "Alice in Wonderland")
        self.assertEqual(profiles[2002].unique_id, "bob_builder")

    def test_resolve_uids_to_profiles_caches_in_memory(self) -> None:
        calls = []
        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {"im_user_profile": {"user_id": 5005, "unique_id": "david", "nick_name": "Dave"}}
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            calls.append(url)
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=fake",
            http_client=fake_http,
        )
        # First call fetches via HTTP
        p1 = reader.resolve_uids_to_profiles([5005])
        self.assertEqual(len(calls), 1)
        self.assertEqual(p1[5005].unique_id, "david")

        # Second call uses in-memory cache, no HTTP call
        p2 = reader.resolve_uids_to_profiles([5005])
        self.assertEqual(len(calls), 1)
        self.assertEqual(p2[5005].unique_id, "david")

        # Force refresh triggers HTTP call
        p3 = reader.resolve_uids_to_profiles([5005], force=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(p3[5005].unique_id, "david")

    def test_resolve_uids_to_profiles_api_failure_and_fallback(self) -> None:
        calls = []
        profile_json = json.dumps({
            "status_code": 0,
            "users": [{"im_user_profile": {"user_id": 777, "unique_id": "lucky", "nick_name": "Seven"}}],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            calls.append(url)
            if "primary" in url:
                raise RuntimeError("Connection reset")
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=fake",
            profile_endpoint_url="https://primary.example.com/im/user/profile/",
            profile_fallback_endpoints=["https://fallback.example.com/im/user/profile/"],
            http_client=fake_http,
        )
        profiles = reader.resolve_uids_to_profiles([777])
        self.assertEqual(len(calls), 2)
        self.assertIn("primary", calls[0])
        self.assertIn("fallback", calls[1])
        self.assertEqual(profiles[777].unique_id, "lucky")

    def test_resolve_uids_malformed_json_handled_gracefully(self) -> None:
        def fake_http(url: str, **kwargs):
            return 200, {}, b"not json at all"

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=fake",
            http_client=fake_http,
        )
        profiles = reader.resolve_uids_to_profiles([999])
        self.assertEqual(profiles, {})

    def test_refresh_recipients_resolves_usernames_without_explicit_uid(self) -> None:
        streak_data = _build_streak_value(
            users=[1000, 5555],
            streak_counter=123,
            conv_id="0:1:1000:5555",
        )
        streak_resp_bytes = _build_response([streak_data])

        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {
                    "im_user_profile": {
                        "user_id": 5555,
                        "unique_id": "resolved_user",
                        "nick_name": "Resolved Nick",
                    }
                }
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            if "streaks" in url:
                return 200, {}, streak_resp_bytes
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        recipients = [
            {"name": "Some Display Name", "username": "resolved_user"},  # Notice: NO user_id!
        ]

        results = reader.refresh_recipients(recipients, force_refresh=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, StreakStatus.AVAILABLE)
        self.assertEqual(results[0].count, 123)
        self.assertEqual(results[0].user_id, 5555)

        # Confirm persisted in cache with user_id
        cached = self.cache.get("resolved_user")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.count, 123)
        self.assertEqual(cached.user_id, 5555)

    def test_unknown_username_reported_as_unknown_without_inventing_streak(self) -> None:
        streak_data = _build_streak_value(
            users=[1000, 2000],
            streak_counter=45,
            conv_id="0:1:1000:2000",
        )
        streak_resp_bytes = _build_response([streak_data])

        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {"im_user_profile": {"user_id": 2000, "unique_id": "someone_else", "nick_name": "Other"}}
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            if "streaks" in url:
                return 200, {}, streak_resp_bytes
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        recipients = [
            {"name": "Mysterious Person", "username": "non_existent_username"},
        ]

        results = reader.refresh_recipients(recipients, force_refresh=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, StreakStatus.UNKNOWN)
        self.assertIsNone(results[0].count)
        self.assertIsNone(results[0].user_id)

    def test_duplicate_display_names_never_collide(self) -> None:
        # Two users have the EXACT SAME display name: "Bestie"
        # User 1: @bestie_one (UID 1111, streak 100)
        # User 2: @bestie_two (UID 2222, streak 200)
        s1 = _build_streak_value(users=[9999, 1111], streak_counter=100, conv_id="0:1:9999:1111")
        s2 = _build_streak_value(users=[9999, 2222], streak_counter=200, conv_id="0:1:9999:2222")
        streak_resp_bytes = _build_response([s1, s2])

        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {"im_user_profile": {"user_id": 1111, "unique_id": "bestie_one", "nick_name": "Bestie"}},
                {"im_user_profile": {"user_id": 2222, "unique_id": "bestie_two", "nick_name": "Bestie"}},
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            if "streaks" in url:
                return 200, {}, streak_resp_bytes
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        recipients = [
            {"name": "Bestie", "username": "bestie_one"},
            {"name": "Bestie", "username": "bestie_two"},
        ]

        results = reader.refresh_recipients(recipients, force_refresh=True)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].username, "bestie_one")
        self.assertEqual(results[0].count, 100)
        self.assertEqual(results[0].user_id, 1111)

        self.assertEqual(results[1].username, "bestie_two")
        self.assertEqual(results[1].count, 200)
        self.assertEqual(results[1].user_id, 2222)

        # Cache check
        c1 = self.cache.get("bestie_one")
        c2 = self.cache.get("bestie_two")
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        self.assertEqual(c1.count, 100)
        self.assertEqual(c2.count, 200)

    def test_read_recipient_streak_resolves_username(self) -> None:
        streak_data = _build_streak_value(
            users=[1000, 4444],
            streak_counter=77,
            conv_id="0:1:1000:4444",
        )
        streak_resp_bytes = _build_response([streak_data])
        profile_json = json.dumps({
            "status_code": 0,
            "users": [
                {"im_user_profile": {"user_id": 4444, "unique_id": "my_friend", "nick_name": "Friend"}}
            ],
        }).encode("utf-8")

        def fake_http(url: str, **kwargs):
            if "streaks" in url:
                return 200, {}, streak_resp_bytes
            return 200, {}, profile_json

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        rec = reader.read_recipient_streak("my_friend", force_refresh=True)
        self.assertEqual(rec.status, StreakStatus.AVAILABLE)
        self.assertEqual(rec.count, 77)
        self.assertEqual(rec.user_id, 4444)

    def test_refresh_recipients_unknown_with_prior_cache_returns_stale_record(self) -> None:
        prior_record = StreakRecord(
            username="alice",
            status=StreakStatus.AVAILABLE,
            count=42,
            updated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            user_id=12345,
            conversation_id="0:1:9999:12345",
        )
        self.cache.set(prior_record)
        self.assertFalse(self.cache.get("alice").stale)

        other_user = _build_streak_value(
            users=[9999, 88888],
            streak_counter=10,
            conv_id="0:1:9999:88888",
        )
        streak_resp_bytes = _build_response([other_user])

        def fake_http(url: str, **kwargs):
            return 200, {}, streak_resp_bytes

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        recipients = [{"name": "Alice", "username": "alice", "user_id": 12345}]
        results = reader.refresh_recipients(recipients, force_refresh=True)

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res.username, "alice")
        self.assertEqual(res.count, 42)
        self.assertEqual(res.status, StreakStatus.AVAILABLE)
        self.assertTrue(res.stale)

        cached = self.cache.get("alice")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.count, 42)
        self.assertTrue(cached.stale)

    def test_refresh_recipients_unknown_without_prior_cache_returns_unknown(self) -> None:
        streak_resp_bytes = _build_response([])

        def fake_http(url: str, **kwargs):
            return 200, {}, streak_resp_bytes

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        recipients = [{"name": "Charlie", "username": "charlie"}]
        results = reader.refresh_recipients(recipients, force_refresh=True)

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res.username, "charlie")
        self.assertEqual(res.status, StreakStatus.UNKNOWN)
        self.assertIsNone(res.count)
        self.assertFalse(res.stale)

        self.assertIsNone(self.cache.get("charlie"))

    def test_read_recipient_streak_unknown_with_prior_cache_marks_stale_and_returns_stale(self) -> None:
        prior_record = StreakRecord(
            username="bob",
            status=StreakStatus.AVAILABLE,
            count=99,
            updated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            user_id=54321,
            conversation_id="0:1:9999:54321",
        )
        self.cache.set(prior_record)
        self.assertFalse(self.cache.get("bob").stale)

        streak_resp_bytes = _build_response([])

        def fake_http(url: str, **kwargs):
            return 200, {}, streak_resp_bytes

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        rec = reader.read_recipient_streak("bob", force_refresh=True)
        self.assertEqual(rec.username, "bob")
        self.assertEqual(rec.count, 99)
        self.assertEqual(rec.status, StreakStatus.AVAILABLE)
        self.assertTrue(rec.stale)

        cached = self.cache.get("bob")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.count, 99)
        self.assertTrue(cached.stale)

    def test_read_recipient_streak_unknown_without_prior_cache_returns_unknown(self) -> None:
        streak_resp_bytes = _build_response([])

        def fake_http(url: str, **kwargs):
            return 200, {}, streak_resp_bytes

        reader = ApiStreakReader(
            self.cache,
            cookie_header="sessionid=valid",
            http_client=fake_http,
        )

        rec = reader.read_recipient_streak("stranger", force_refresh=True)
        self.assertEqual(rec.username, "stranger")
        self.assertEqual(rec.status, StreakStatus.UNKNOWN)
        self.assertIsNone(rec.count)
        self.assertFalse(rec.stale)

        self.assertIsNone(self.cache.get("stranger"))


if __name__ == "__main__":
    unittest.main()
