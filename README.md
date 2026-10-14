# Daily Streak Tool v2

Tự động gửi video TikTok hàng ngày đến danh sách bạn bè để duy trì streak.

## Tính năng

- Giao diện UI (Tkinter) để quản lý danh sách người nhận
- Tự động scan danh sách DM và resolve username
- Hỗ trợ đa nền tảng: Linux, Windows, macOS
- Lên lịch gửi tự động hàng ngày theo giờ cố định
- Inject cookie từ file (không cần login lại mỗi lần)
- Tự động xử lý Screen Time popup, Sleep Hours popup
- Tuy nhiên, người dùng cần phải tự tay xử lý captcha khi trang yêu cầu.

## Yêu cầu

- Python 3.10+
- Google Chrome đã cài đặt
- TikTok account đã đăng nhập

## Cài đặt

```bash
# Clone repo
git clone https://github.com/w111user/daily-streak-tool
cd daily-streak-tool

# Tạo virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows

# Cài core/server dependencies (không cần Tkinter hoặc desktop notifier)
pip install .
playwright install chromium
```

Desktop notifications và Telegram notifications là optional. Development
workflow cũ vẫn dùng được với `pip install -r requirements.txt`. Khi cài từ
package, dùng `pip install '.[desktop,telegram]'` nếu cần các tính năng đó.

The optional SSH-friendly Textual frontend is installed separately with:

```bash
pip install '.[tui]'
```

### Local Kali Linux deployment

The primary supported runtime is a local Kali Linux machine using this
project's `.venv`, Playwright installation, and system Google Chrome. Always
use that environment for browser operations rather than a system Python or a
Node binary found through `PATH`:

```bash
./.venv/bin/python -V
./.venv/bin/streak --config config.json doctor
./.venv/bin/streak --config config.json tui
./.venv/bin/streak --config config.json daemon
```

Keep one local `config.json`. For a normal local headless deployment with an
exported cookie file, its `tiktok` section should explicitly use:

```json
{
  "headless": true,
  "persistent_context": false,
  "low_memory_mode": false
}
```

The normal DM-readiness budget, bounded DM page recovery, and recipient retry
defaults remain in effect when their optional settings are absent. The
low-memory mode and longer diagnostic budgets are retained only as optional
compatibility settings; they are not the recommended local Kali configuration.
Do not commit the real `config.json`, cookie export, browser profile, or any
Playwright storage-state file.

## Cấu hình

### 1. Export cookie từ TikTok

- Cài extension [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie-v3/ojfebgpkimhlhcblbalbfjblapadhbol)
- Đăng nhập TikTok trên Chrome
- Click extension → Export → lưu thành `cookies.json` vào thư mục project

### 2. Cấu hình `config.json`

Tạo file `config.json` từ mẫu `config.example`:

```json
{
  "schedule": {
    "time": "00:00",
    "timezone": "Asia/Ho_Chi_Minh",
    "run_on_start": true
  },
  "cookie_file": "cookies.json",
  "tiktok": {
    "headless": false,
    "user_data_dir_linux": "~/chrome-debug",
    "user_data_dir_macos": "~/Library/Application Support/chrome-debug",
    "user_data_dir_windows": "~\\AppData\\Local\\chrome-debug",
    "message_delay_seconds": [8, 18],
    "navigation_timeout_ms": 60000,
    "action_timeout_ms": 10000,
    "content_ready_timeout_ms": 45000,
    "dm_readiness_timeout_ms": 45000,
    "dm_load_attempts": 2,
    "probe_timeout_ms": 750,
    "low_memory_mode": false,
    "low_memory_stabilization_seconds": 4.0,
    "persistent_context": true,
    "recipient_retries": 1
  },
  "runtime": {
    "run_lock_stale_seconds": 21600
  },
  "recipients": [],
  "videos": [
    "https://www.tiktok.com/@username/video/..."
  ],
  "telegram": {
    "enabled": false,
    "bot_token": "",
    "chat_id": ""
  }
}
```

### Browser modes

`tiktok.headless` is honored by both the sender and the Tkinter DM tools:

- `false` (the default) opens a visible browser window.
- `true` runs without a visible browser; use valid cookies and be aware that a
  CAPTCHA or interactive login cannot be completed from that process.
- On a Linux server that needs a visible browser without a physical display,
  keep `headless` as `false` and run the existing command under a virtual
  display, for example `xvfb-run -a python3 main.py`.

Chrome and Chromium are detected from common platform locations and `PATH`. If
needed, add the optional `tiktok.browser_executable` setting with an absolute
executable path. The existing profile-directory settings remain unchanged.

Timeouts remain deliberately split: navigation defaults to 60 seconds,
ordinary actions to 10 seconds, heavy TikTok content readiness to 45 seconds,
and passive CAPTCHA/popup probes to 750 milliseconds. All timeout settings are
optional and retain these defaults when absent from an existing config.

`tiktok.recipient_retries` is optional and defaults to `1`, meaning a failed
recipient operation is retried once (at most two attempts). Failures are
reported per recipient and do not stop later recipients in the same batch.

`tiktok.low_memory_mode` is optional and defaults to `false`. When enabled for
a constrained server, Chromium keeps normal scripts, stylesheets, fetch, and
XHR traffic available but blocks images, media, and fonts. It also adds only
the conservative `--disable-gpu`, `--disable-dev-shm-usage`, and
`--no-first-run` launch flags. This mode applies to shared browser sessions,
including CLI, TUI, daemon, and Tkinter actions when explicitly enabled.
When that mode first obtains a `loading` DOM execution context, it waits
`low_memory_stabilization_seconds` (default `4.0`) before beginning the
separate 45-second DM readiness window.

`tiktok.persistent_context` is optional and defaults to `true` for existing
desktop behavior. A headless server that supplies an exported cookie file on
each run can use `false`; it launches Chromium without a user-data directory,
creates a temporary browser context, injects cookies, and closes all browser
resources at the end of the operation. This pairs well with
`headless: true` and `low_memory_mode: true` on constrained servers.

`runtime.run_lock_stale_seconds` is optional and defaults to six hours. It is
only used to recover a lock left behind by a dead process; a live local sender
process is never treated as stale solely because it has been running for a
long time.

### Daily resume state and history

Successful recipients are recorded immediately, so an interrupted run skips
only recipients already sent on the current schedule-local day. Failed
recipients remain eligible on the next run. State and `history.jsonl` are kept
outside this repository in a per-user data location:

- Linux: `$XDG_STATE_HOME/daily-streak-tool` or `~/.local/state/daily-streak-tool`
- macOS: `~/Library/Application Support/daily-streak-tool`
- Windows: `%LOCALAPPDATA%\daily-streak-tool`

Set `DAILY_STREAK_DATA_DIR` to override that location, for example for server
deployment. The optional top-level `data_dir` or `state.data_dir` config value
also overrides the platform default when the environment variable is absent.

## Sử dụng

### Chạy UI (khuyến nghị)

```bash
python3 ui.py
```

1. Điền thông tin config (giờ gửi, delay, video links)
2. Click **Scan DM List** để lấy danh sách người nhận
3. Click **Resolve Usernames** để lấy username thật
4. Tick chọn người muốn gửi
5. Click **Start Sender**

### Chạy trực tiếp

```bash
python3 main.py
```

### CLI và server daemon

The installable `streak` command uses the same sender, recipient, state, and
history core as the GUI. These inspection commands never launch a browser:

```bash
streak status
streak history --limit 10
streak doctor
streak recipients list
streak videos list
```

Use `--json` for script-friendly output; JSON is the only content written to
stdout, while diagnostics continue to stderr:

```bash
streak status --json
streak doctor --json
streak history --json
```

Send normally with interruption-safe daily resume, or explicitly resend the
whole configured batch for the day:

```bash
streak send
streak send --force
```

`streak recipients scan` and `streak recipients resolve` launch the shared
browser/recipient service and print their findings. They deliberately do not
overwrite `config.json`; review their output and update the recipient list in
the GUI or configuration file when appropriate.

Run the scheduler in the foreground under systemd, Docker, tmux, or another
supervisor:

```bash
streak daemon
```

The daemon reloads `config.json` before every batch, handles SIGINT/SIGTERM,
and rejects overlapping work in-process. The shared sender also uses a
cross-process lock in the data directory, so a manual `streak send` cannot
race a daemon batch. `python3 main.py` remains a compatible alias for this
daemon behavior.

CLI exits with `0` for a completed batch without failed recipients, `1` when
one or more recipient sends fail, `2` for configuration or usage problems,
`3` for runtime/setup failures, and `130` for an interruption.

For a headed browser on a Linux server without a physical display, use a
virtual display with either entry point:

```bash
xvfb-run -a streak daemon
```

### Textual TUI (optional)

The TUI is an optional terminal frontend; it uses the same configuration,
sender, recipient, state, history, browser, and run-lock core as the CLI and
Tkinter UI. It does not replace the Tkinter interface.

```bash
streak tui
streak --config /path/to/config.json tui
```

It provides Dashboard, Recipients, Videos, History, current `RecipientResult`
rows, completed `RunSummary` counts, and a capped live-log pane. Browser
processes are created only for **Send**, **Scan**, and **Resolve**, then their
shared contexts are closed; the TUI does not keep Chromium running while idle.

Keyboard actions are shown in the footer:

- `s` send with normal daily resume; `f` force send.
- `c` scan DM conversations; `r` resolve selected usernames.
- `Space` select/deselect the highlighted recipient; `a` select all; `d`
  deselect all. Selection changes are atomically saved to `config.json` and
  preserve unrelated settings.
- `g` refresh local config/state/history; `q` quit.

New recipients discovered by scan are deliberately unselected until you select
them. Scan and resolve only open the browser for their action; refresh, tabs,
history, videos, and dashboard remain browser-free.

## Lưu ý

- `cookies.json` hết hạn sau vài tháng → cần export lại
- Không commit `cookies.json` và `config.json` lên GitHub
- Giữ process chạy liên tục để scheduler hoạt động (dùng `tmux` hoặc `screen` trên Linux)

> ⚠️ **Disclaimer:** This project is for educational purposes only. 
> Automated interaction with TikTok may violate their Terms of Service.
> Use at your own risk.
