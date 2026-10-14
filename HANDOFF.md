# Daily Streak Tool v2 handoff for Antigravity

## Scope and current deployment target

The canonical runtime is now the **local Kali Linux development machine**.
Do not continue ARM/Armbian performance work or refactor the TikTok
`/messages` readiness loader without new local evidence.

This repository is a v2 refactor of a TikTok daily-streak tool. It sends a
configured TikTok video to configured direct-message recipients on a schedule.
It supports the legacy Tkinter GUI, an installable CLI, an optional Textual
TUI, and a foreground daemon. The shared core is the source of behavior;
adapters must remain thin.

## Architecture

### Entry points and adapters

- `ui.py`: legacy-compatible Tkinter desktop entry point. Keep `python3 ui.py`
  working; UI code delegates browser/recipient/sender behavior to the package.
- `main.py`: legacy-compatible daemon entry point. Keep `python3 main.py`
  working.
- `daily_streak.cli`: implements the `streak` console command. Browser-free
  commands must not import Tkinter, Playwright sender code, or desktop
  notification packages.
- `daily_streak.tui`: optional Textual frontend for `streak tui`. Its
  `TuiController` is Textual-free and calls shared core services. Textual is
  imported only when the TUI is actually invoked. Browser contexts must not
  remain alive while the TUI is idle.
- `daily_streak.daemon`: foreground scheduler for `streak daemon` and
  `main.py`; reloads config before every batch and relies on the shared sender
  lock.

### Shared core

- `daily_streak.config`: one implementation for config paths, defaults, JSON
  loading, and atomic saving.
- `daily_streak.browser`: browser executable discovery, launch configuration,
  persistent/ephemeral contexts, timeout classes, cookie conversion/loading,
  optional low-memory routing, and ordered cleanup.
- `daily_streak.navigation`: shared TikTok navigation milestones (notably
  `/messages` commit navigation followed by explicit readiness).
- `daily_streak.recipients`: DM scanning, exact conversation matching,
  username resolution from chat headers, and shared bounded `/messages`
  renderer recovery.
- `daily_streak.recipient_actions`: short-lived browser wrappers used by CLI
  and TUI scan/resolve actions; always closes the temporary session.
- `daily_streak.sender`: batch send orchestration, per-recipient results and
  retries, video selection, persistent state/history writes, and run-lock use.
- `daily_streak.models`: typed `RecipientResult`, `RunSummary`, and statuses.
- `daily_streak.videos`: `VideoPool` validation and selection; root
  `video_pool.py` remains a compatibility import.
- `daily_streak.state`, `daily_streak.history`, and `daily_streak.paths`:
  per-user durable daily completion state, run history, and data-directory
  selection.
- `daily_streak.lock`: shared cross-process sender lock.
- `daily_streak.tiktok`: CAPTCHA and TikTok popup helpers.
- `daily_streak.notifications`: lazy desktop/Telegram notifications; root
  `notifier.py` remains a compatibility import.
- `daily_streak.doctor`, `daily_streak.schedule`: offline setup diagnostics and
  schedule parsing/next-run calculations.

## Verified local environment

All browser diagnostics must use the project virtual environment, never a
PATH-resolved Python or Node runtime.

- OS: Kali Linux
- Project venv: `.venv`
- Python: 3.14.6
- Playwright Python: 1.63.0
- Bundled Playwright Node: v24.21.0
- Playwright CLI: 1.63.0
- System browser: `/usr/bin/google-chrome`
- Headless Playwright `about:blank` smoke test: passed in 1.16 seconds

The local browser diagnostics used an in-memory, non-persistent override:
`headless=true`, `persistent_context=false`, `low_memory_mode=false`.
No real config was changed for those checks.

The actual current local `config.json` has `headless=true` and
`low_memory_mode` unset (therefore false), but it omits `persistent_context`.
It consequently retains the legacy default of `true`. Do not silently edit
this user file; if ephemeral local sessions are desired for normal use, make
that deliberate user-approved configuration change in the one real config.

## Configuration and secret handling

- Operational config is `config.json` in the project root unless `--config`
  supplies another path.
- The authenticated cookie export is local and secret. Never print its
  contents, token values, browser storage, or credential material; never add
  them to tests, logs, commits, or reports.
- Do not delete, overwrite, relocate, or normalize user cookies, config,
  profiles, or credentials during refactors.
- `.gitignore` excludes `config.json`, cookie exports, Playwright storage
  state, local profiles, local runtime directories, and package build output.
- State/history default outside the repo to the platform user-data directory;
  `DAILY_STREAK_DATA_DIR`, top-level `data_dir`, or `state.data_dir` can
  override that location.

## Commands

Run from the project root.

```bash
# Installation / development environment
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[tui,desktop,telegram]'
./.venv/bin/playwright install chromium

# Offline validation
./.venv/bin/python -m compileall -q .
./.venv/bin/python -m unittest discover -v
git diff --check

# Browser-free local diagnostic
./.venv/bin/streak --config config.json doctor

# Interactive local adapters
./.venv/bin/streak --config config.json tui
./.venv/bin/streak --config config.json daemon

# Legacy-compatible entry points
./.venv/bin/python ui.py
./.venv/bin/python main.py
```

`streak daemon` is a foreground process suitable for a local terminal, tmux,
or a supervisor. Do not start or enable it automatically. The current local
config reports `schedule.run_on_start=false`, so daemon startup does not
immediately invoke a send; scheduled sends still require a deliberately
verified configuration.

## Verification evidence

### Offline checks (latest run)

```bash
./.venv/bin/python -m compileall -q .
./.venv/bin/python -m unittest discover -v
git diff --check
```

- `compileall`: passed.
- Unit tests: **158 passed** in ~7.9 seconds.
- `git diff --check`: passed.
- `./.venv/bin/streak --config config.json doctor`: **11 pass, 0 warn,
  0 fail** in the real local environment.
- The optional Textual package is installed. TUI Dashboard, Recipients,
  Videos, History, and Daemon tabs verified.
- Telegram batch-completion notifications verified for automated daemon
  batches and manual TUI Send/Force Send operations.

Unit tests use fakes/mocks and must never need a TikTok login, a real cookie,
or network access.

### Live verification evidence

- Live delivery verification: verified on deployment machine; outgoing message
  detection verifies sent video link/ID in TikTok DM and records atomic daily
  state and history.
- Scheduled daemon batch: completed full run with 18 successes, 0 failures,
  0 unconfirmed, 0 skipped.
- Telegram batch summary: successfully received batch-completion notification.
- Daemon lifecycle: detached daemon start, persistent execution across TUI
  close/reopen, and graceful stop verified.

## Operational constraints

1. Operational config is `config.json` in the project root unless `--config`
   supplies another path.
2. The authenticated cookie export is local and secret. Never print its
   contents, token values, browser storage, or credential material; never add
   them to tests, logs, commits, or reports.
3. `.gitignore` excludes `config.json`, cookie exports, Playwright storage
   state, local profiles, local runtime directories, and package build output.
4. State/history default outside the repo to the platform user-data directory;
   `DAILY_STREAK_DATA_DIR`, top-level `data_dir`, or `state.data_dir` can
   override that location.
5. All sender entry points acquire the shared run lock before session setup.
   Cross-process locking prevents duplicate concurrent sends between CLI,
   TUI, and daemon.

## Worktree and handoff constraints

This is intentionally a dirty worktree containing the completed v2 phase
work. Do not use `git reset --hard`, `git checkout --`, or broad cleanup.

At handoff, the changed/untracked v2 implementation set is:

- Modified tracked files: `.gitignore`, `README.md`, `captcha.py`,
  `config.example`, `main.py`, `notifier.py`, `sender.py`, `ui.py`, and
  `video_pool.py`.
- Untracked v2 files/directories: `AGENTS.md`, `daily_streak/`,
  `pyproject.toml`, `tests/`, and this `HANDOFF.md`.

This milestone specifically added the local Kali documentation and ignore
rules in `README.md` and `.gitignore`; do not discard the broader uncommitted
v2 work, which is intentional and covered by the test suite.

Always preserve existing GUI/CLI/TUI/daemon and legacy entry-point behavior,
avoid unnecessary rewrites, keep browser/session/configuration code shared,
and do not perform a live send or start the daemon unless the user explicitly
asks.
