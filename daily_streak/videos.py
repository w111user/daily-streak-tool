import logging
import random
import re
from typing import Any, Mapping
import urllib.request


logger = logging.getLogger(__name__)

_CANONICAL_CACHE: dict[str, tuple[str, str]] = {}


def extract_canonical_video_id(video_url: str) -> str:
    """Extract a numeric video ID or fallback identifier from a TikTok URL."""
    cleaned = video_url.strip().split("?")[0].rstrip("/")
    match = re.search(r"/video/(\d+)", cleaned)
    if match:
        return match.group(1)
    if "vt.tiktok.com/" in cleaned:
        return cleaned.split("vt.tiktok.com/")[-1].split("/")[0]
    if "/v/" in cleaned:
        part = cleaned.split("/v/")[-1].split("/")[0]
        return part.replace(".html", "")
    return cleaned


def resolve_canonical_video(video_url: str, timeout_seconds: float = 10.0) -> tuple[str, str]:
    """Resolve a video URL (including vt.tiktok.com short links) to (canonical_url, video_id)."""
    cleaned = video_url.strip()
    if cleaned in _CANONICAL_CACHE:
        return _CANONICAL_CACHE[cleaned]

    # If it's already a full video link with an ID, no network request needed
    if "/video/" in cleaned:
        vid = extract_canonical_video_id(cleaned)
        _CANONICAL_CACHE[cleaned] = (cleaned, vid)
        return cleaned, vid

    # If it's a short link or redirect link, resolve via HTTP redirect
    try:
        req = urllib.request.Request(
            cleaned,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            final_url = resp.geturl()
            vid = extract_canonical_video_id(final_url)
            _CANONICAL_CACHE[cleaned] = (final_url, vid)
            return final_url, vid
    except Exception as exc:
        logger.debug("Could not resolve short video URL %s via network: %s", cleaned, exc)
        fallback_id = extract_canonical_video_id(cleaned)
        _CANONICAL_CACHE[cleaned] = (cleaned, fallback_id)
        return cleaned, fallback_id


class VideoPool:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.videos = [
            video.strip()
            for video in config.get("videos", [])
            if isinstance(video, str) and video.strip()
        ]

    def random_video(self) -> str:
        if not self.videos:
            raise ValueError("No videos configured. Add links to the 'videos' list in config.json.")
        return random.choice(self.videos)
