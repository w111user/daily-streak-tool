# Daily Streak Tool contributor guide

## Purpose

Daily Streak Tool sends a selected TikTok video to configured direct-message
recipients on a schedule to help maintain daily streaks. Version 2 provides a
Tkinter desktop interface, an installable CLI, and a foreground server daemon
without changing the sender's core behavior.

## Current architecture

- `ui.py` is the Tkinter desktop entry point. It edits `config.json`, scans DM
  conversations, resolves usernames, and starts a sender on a background
  asyncio loop.
- `daily_streak.cli` provides the `streak` command. Browser-free inspection
  commands must not import Tkinter, Plyer, or Playwright sender code.
- `daily_streak.tui` is an optional Textual adapter for `streak tui`. Keep its
  controller Textual-free and import Textual only after the `tui` command is
  invoked. It must call shared sender/recipient/config/state/history code.
- `daily_streak.daemon` is the foreground scheduler used by `streak daemon`
  and the legacy `main.py`; it reloads configuration before each batch.
- `daily_streak.lock` owns the shared cross-process sender lock.
- `daily_streak.sender` contains the Playwright sending flow; root `sender.py`
  preserves legacy imports.
- `daily_streak.recipients` handles scanning, conversation matching, and
  username resolution; the Tkinter UI supplies only browser lifecycle and UI
  status updates.
- `daily_streak.tiktok` handles CAPTCHA waiting and TikTok popups; root
  `captcha.py` preserves legacy imports.
- `daily_streak.notifications` supplies optional desktop and Telegram
  notifications with lazy third-party imports; root `notifier.py` is a
  compatibility import.
- `daily_streak.videos` validates and selects video links; root
  `video_pool.py` preserves legacy imports.
- `daily_streak.paths`, `daily_streak.state`, and `daily_streak.history` own
  per-user data locations, resume-safe daily completion state, and durable run
  summaries.
- `daily_streak/` is the v2 shared package. It contains configuration,
  browser/session utilities, recipient operations, TikTok interaction helpers,
  result models, video selection, and the sender core; legacy modules remain
  supported as compatibility imports.

## V2 architecture rules

- Keep GUI, CLI, and daemon adapters thin. Put shared behavior in
  `daily_streak` core modules, never in a user interface.
- Browser executable discovery, persistent-profile selection, launch options,
  and cookie conversion/loading have one implementation in
  `daily_streak.browser`.
- Configuration paths, defaults, and JSON read/write behavior have one
  implementation in `daily_streak.config`.
- Future sender work must isolate each recipient failure and record explicit
  delivery outcomes; it must not infer confirmed delivery solely from an Enter
  keypress.
- Daily-state and history logic must remain persistent and shared by GUI, CLI,
  and daemon. Do not implement adapter-specific state stores.
- All sender entry points must use the shared run lock before session setup.
  Never add a CLI-only lock or bypass the lock for daemon execution.
- Keep CLI JSON stdout strictly machine-readable. Logging and progress belong
  on stderr, and browser-free commands must keep their imports browser-free.
- TUI actions must not keep browser contexts alive between operations. Its
  live-log buffer must remain bounded for low-memory SSH servers.
- Keep Playwright and Tkinter out of pure configuration/state/recipient/video
  logic so those modules can be unit-tested without a live TikTok session.

## Compatibility and safety constraints

- Preserve `python3 ui.py` and `python3 main.py` workflows while the package
  API is introduced.
- Continue accepting the existing `config.json` keys, including platform
  profile directory keys, `tiktok.headless`, recipient objects, video lists,
  and Telegram settings. New keys must be optional.
- Never modify, print, commit, or add tests that depend on user `config.json`,
  cookie exports, browser profiles, or credentials.
- Headed sessions, `headless: true`, and headed Linux sessions under a supplied
  virtual display (for example `xvfb-run`) must remain viable.
- Do not remove legacy behavior merely because it appears unused; first locate
  callers and preserve a compatible wrapper when needed.

## Coding and testing conventions

- Target Python 3.10+ and use type annotations for public functions.
- Prefer `pathlib.Path`, explicit error handling, and standard-library unit
  tests. Keep new dependencies deliberate and documented in `pyproject.toml`.
- Test pure logic and fake browser contexts; never automate a live TikTok
  account in the test suite.
- Run `python3 -m unittest discover -v` and `python3 -m compileall -q .` after
  changes. Import checks requiring optional runtime dependencies should be
  reported clearly when those dependencies are absent.

## V2 delivery roadmap

1. **Foundation (Phase 0–1):** document the system, package shared config and
   browser/session helpers, retain legacy entry points, and add unit tests.
2. **Core extraction and batch outcomes (Phase 2):** shared sender/recipient
   services, video selection, per-recipient retries, and run summaries.
3. **Persistence and resume (Phase 3):** atomic daily state,
   interrupted-run resume, duplicate prevention, and run history.
4. **CLI and daemon (Phase 4):** provide `streak` (`status`, `send`, `daemon`,
   `recipients`, `videos`, `history`, and `doctor`), fresh daemon configuration,
   headless-safe import boundaries, and cross-process run locking.
5. **Optional Textual TUI:** provide `streak tui` over the same core with
   dashboard, action controls, bounded live logs, and no idle browser.
6. **Reliable delivery:** add send verification and configurable popup policy.
7. **Hardening:** broaden tests, document deployment/headless/Xvfb workflows,
   and package release artifacts.
