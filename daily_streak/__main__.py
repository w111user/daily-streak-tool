"""Run the command-line interface with ``python -m daily_streak``."""

from __future__ import annotations

import sys

from daily_streak.cli import main


if __name__ == "__main__":
    sys.exit(main())
