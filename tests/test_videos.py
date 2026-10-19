from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from daily_streak.videos import (
    _CANONICAL_CACHE,
    VideoPool,
    extract_canonical_video_id,
    resolve_canonical_video,
)


class VideoPoolAndResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        _CANONICAL_CACHE.clear()

    def test_extract_canonical_video_id(self) -> None:
        self.assertEqual(
            extract_canonical_video_id("https://www.tiktok.com/@user/video/7692053355551526162"),
            "7692053355551526162",
        )
        self.assertEqual(
            extract_canonical_video_id(
                "https://www.tiktok.com/@user/video/7692053355551526162?is_from_webapp=1&sender_device=pc"
            ),
            "7692053355551526162",
        )
        self.assertEqual(
            extract_canonical_video_id("https://vt.tiktok.com/ZSCaGfTRS/"),
            "ZSCaGfTRS",
        )
        self.assertEqual(
            extract_canonical_video_id("https://m.tiktok.com/v/7692053355551526162.html"),
            "7692053355551526162",
        )

    def test_resolve_canonical_video_direct_video_url(self) -> None:
        url = "https://www.tiktok.com/@khoa/video/7692053355551526162?extra=1"
        res_url, vid = resolve_canonical_video(url)
        self.assertEqual(res_url, url)
        self.assertEqual(vid, "7692053355551526162")
        self.assertIn(url, _CANONICAL_CACHE)

    @patch("urllib.request.urlopen")
    def test_resolve_canonical_video_with_redirect(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.geturl.return_value = "https://www.tiktok.com/@khoa/video/7692053355551526162"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        short_url = "https://vt.tiktok.com/ZSCaGfTRS/"
        res_url, vid = resolve_canonical_video(short_url)
        self.assertEqual(res_url, "https://www.tiktok.com/@khoa/video/7692053355551526162")
        self.assertEqual(vid, "7692053355551526162")
        self.assertIn(short_url, _CANONICAL_CACHE)

        # Subsequent call should use cache
        mock_urlopen.reset_mock()
        res_url2, vid2 = resolve_canonical_video(short_url)
        self.assertEqual(res_url2, res_url)
        self.assertEqual(vid2, vid)
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen", side_effect=Exception("network down"))
    def test_resolve_canonical_video_network_failure_fallback(self, mock_urlopen: MagicMock) -> None:
        short_url = "https://vt.tiktok.com/ZSCaGfTRS/"
        res_url, vid = resolve_canonical_video(short_url)
        self.assertEqual(res_url, short_url)
        self.assertEqual(vid, "ZSCaGfTRS")

    def test_video_pool_random_choice(self) -> None:
        config = {
            "videos": [
                "https://www.tiktok.com/@user/video/111",
                "https://www.tiktok.com/@user/video/222",
            ]
        }
        pool = VideoPool(config)
        self.assertEqual(len(pool.videos), 2)
        choice = pool.random_video()
        self.assertIn(choice, pool.videos)

    def test_video_pool_empty_raises(self) -> None:
        pool = VideoPool({"videos": []})
        with self.assertRaises(ValueError):
            pool.random_video()
