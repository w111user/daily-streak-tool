import random
from typing import Any


class VideoPool:
    def __init__(self, config: dict[str, Any]) -> None:
        self.videos = [
            video.strip()
            for video in config.get("videos", [])
            if isinstance(video, str) and video.strip()
        ]

    def random_video(self) -> str:
        if not self.videos:
            raise ValueError("No videos configured. Add links to the 'videos' list in config.json.")
        return random.choice(self.videos)
