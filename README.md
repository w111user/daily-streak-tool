# Daily Streak Tool v2

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Playwright](https://img.shields.io/badge/playwright-chromium-green.svg)](https://playwright.dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)
[![Tests: 158 Passed](https://img.shields.io/badge/tests-158%20passed-brightgreen.svg)](tests/)

**Daily Streak Tool v2** là công cụ tự động hóa gửi video TikTok trực tiếp qua Direct Message (DM) theo lịch trình để duy trì chuỗi tương tác (streaks) với bạn bè.

Phiên bản 2 (v2) được tái cấu trúc toàn diện thành kiến trúc module dùng chung (shared core), hỗ trợ:
- **CLI (`streak`)**: Kiểm tra hệ thống, quét DM, resolve username, và gửi batch tin cậy trên môi trường server.
- **Textual TUI (`streak tui`)**: Giao diện terminal trực quan qua SSH, tích hợp điều khiển daemon, quản lý danh sách người nhận và live logs.
- **Foreground & Detached Daemon**: Chạy lịch trình tự động với cross-process lock an toàn, tự động nạp cấu hình mới.
- **Xác thực gửi thực tế (Delivery Verification)**: Kiểm tra tin nhắn chiều đi (outgoing bubble) thực sự xuất hiện trong DOM trò chuyện thay vì suy đoán từ phím Enter.
- **Telegram Notifications**: Thông báo trạng thái đăng nhập, cảnh báo CAPTCHA, và tổng kết kết quả batch tự động.
- **Giao diện Desktop cũ (`ui.py`)**: Giữ nguyên khả năng tương thích với desktop Tkinter.

---

## Mục lục / Table of Contents

1. [Tính năng nổi bật / Key Features](#1-tính-năng-nổi-bật--key-features)
2. [Kiến trúc hệ thống / Architecture Overview](#2-kiến-trúc-hệ-thống--architecture-overview)
3. [Yêu cầu hệ thống / Requirements & Environments](#3-yêu-cầu-hệ-thống--requirements--environments)
4. [Cài đặt / Installation](#4-cài-đặt--installation)
5. [Cấu hình trình duyệt / Browser Setup](#5-cấu-hình-trình-duyệt--browser-setup)
6. [Export Cookie TikTok an toàn / Secure Cookie Export](#6-export-cookie-tiktok-an-toàn--secure-cookie-export)
7. [Bảng tham số cấu hình / Configuration Reference](#7-bảng-tham-số-cấu-hình--configuration-reference)
8. [Hướng dẫn bắt đầu nhanh / Quick Start](#8-hướng-dẫn-bắt-đầu-nhanh--quick-start)
9. [Tra cứu lệnh CLI / CLI Command Reference](#9-tra-cứu-lệnh-cli--cli-command-reference)
10. [Giao diện dòng lệnh TUI / Textual TUI Reference](#10-giao-diện-dòng-lệnh-tui--textual-tui-reference)
11. [Vận hành Daemon / Daemon Lifecycle & Supervision](#11-vận-hành-daemon--daemon-lifecycle--supervision)
12. [Cơ chế xác thực gửi tin / Delivery Verification](#12-cơ-chế-xác-thực-gửi-tin--delivery-verification)
13. [Thông báo Telegram & Desktop / Notifications](#13-thông-báo-telegram--desktop--notifications)
14. [Quản lý trạng thái & Lịch sử / State & History](#14-quản-lý-trạng-thái--lịch-sử--state--history)
15. [Phát triển & Kiểm thử / Development & Testing](#15-phát-triển--kiểm-thử--development--testing)
16. [Xử lý sự cố & Bảo mật / Troubleshooting & Security](#16-xử-lý-sự-cố--bảo-mật--troubleshooting--security)
17. [Điều khoản sử dụng / Disclaimer](#17-điều-khoản-sử-dụng--disclaimer)

---

## 1. Tính năng nổi bật / Key Features

- **Xác thực gửi tin nhắn chiều đi (Bounded Outgoing Verification)**: Chỉ ghi nhận thành công khi quan sát thấy video ID hoặc link gửi đi xuất hiện trong bong bóng chat của người gửi.
- **Tiếp tục gửi an toàn sau sự cố (Interruption-Safe Daily Resume)**: Tự động lưu tiến độ sau mỗi người nhận; nếu tiến trình bị dừng giữa chừng, lần chạy tiếp theo trong cùng ngày sẽ bỏ qua những người đã nhận thành công.
- **Phân tách lỗi độc lập (Per-Recipient Isolation & Retries)**: Một người nhận bị lỗi không làm dừng cả batch; hỗ trợ cấu hình số lần retry trước khi đánh dấu thất bại.
- **Quản lý khóa đa tiến trình (Cross-Process Run Lock)**: Tránh xung đột gửi trùng lặp giữa CLI, TUI và Daemon bằng khóa độc quyền trong thư mục dữ liệu người dùng.
- **Tùy chọn hiển thị đa dạng**: Headed (mở cửa sổ trực quan), Headless (chạy ngầm không giao diện), và hỗ trợ Xvfb trên Linux server.
- **Tiết kiệm tài nguyên (Low-Memory Mode & Ephemeral Context)**: Tùy chọn chặn tải ảnh/video/font và dọn dẹp browser context ngay sau khi hoàn thành thao tác.
- **Thông báo tổng kết Telegram tự động**: Gửi báo cáo kết quả chi tiết (Success / Failed / Unconfirmed / Skipped) ngay sau khi batch Daemon hoặc manual TUI Send/Force Send hoàn thành.

---

## 2. Kiến trúc hệ thống / Architecture Overview

```text
┌────────────────────────────────────────────────────────────────────────┐
│                       Entry Points & Interfaces                        │
├─────────────────┬──────────────────┬─────────────────┬─────────────────┤
│   CLI: streak   │ TUI: streak tui  │ Daemon: daemon  │ Desktop: ui.py  │
└────────┬────────┴────────┬─────────┴────────┬────────┴────────┬────────┘
         │                 │                  │                 │
         └─────────────────┼──────────────────┼─────────────────┘
                           ▼                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        daily_streak Shared Core                        │
├──────────────────────────┬─────────────────────────┬───────────────────┤
│ Config: daily_streak.config │ Models: daily_streak.models │ Lock: run / daemon│
├──────────────────────────┼─────────────────────────┼───────────────────┤
│ Browser: launch & cookies│ State: DailyStateStore  │ History: RunHistory│
├──────────────────────────┼─────────────────────────┼───────────────────┤
│ Recipients: scan/resolve │ Sender: TikTokSender    │ Notifier: Telegram│
└──────────────────────────┴─────────────────────────┴───────────────────┘
```

- **Core Module (`daily_streak/`)**: Chứa toàn bộ logic đọc/ghi cấu hình, quản lý Chromium, navigation, DOM selectors, retry, xác thực tin nhắn, lưu trữ lịch sử và gửi thông báo.
- **Adapters**:
  - `daily_streak.cli`: Cung cấp lệnh `streak`. Các lệnh tra cứu (`status`, `doctor`, `history`, `videos list`) hoạt động offline và không bao giờ khởi động browser.
  - `daily_streak.tui`: Adapter Textual chạy trên terminal, không phụ thuộc Playwright/Tkinter khi khởi động, chỉ mở browser tạm thời khi bấm Send/Scan/Resolve.
  - `daily_streak.daemon`: Bộ lập lịch tự động dựa trên APScheduler, tự nạp lại cấu hình trước mỗi batch.
  - `ui.py` & `main.py`: Các file tương thích giữ nguyên để người dùng cũ tiếp tục chạy lệnh trực tiếp.

---

## 3. Yêu cầu hệ thống / Requirements & Environments

| Môi trường / Environment | Trạng thái / Support Status | Ghi chú / Notes |
| :--- | :--- | :--- |
| **Debian / Ubuntu / Kali Linux** | **Chính thức & Đã kiểm thử thực tế (Primary & Production-tested)** | Nền tảng phát triển và triển khai chính; kiểm thử với 158 automated tests và live message delivery. |
| **Arch Linux / Fedora / RHEL** | Tương thích (Compatible) | Chạy tốt với Python 3.10+ và Google Chrome / Chromium. |
| **macOS** | Tương thích kế thừa (Compatibility) | Hỗ trợ desktop notifications qua AppleScript và profile Chrome macOS. |
| **Windows 10 / 11** | Tương thích kế thừa (Compatibility) | Hỗ trợ đường dẫn Chrome Windows và pywin32 (`[windows]` extra). |

**Yêu cầu phần mềm:**
- Python `>= 3.10`
- Trình duyệt **Google Chrome** hoặc **Chromium** (hoặc Playwright-managed Chromium)
- Tài khoản TikTok đã đăng nhập sẵn trên trình duyệt

---

## 4. Cài đặt / Installation

### Bước 1: Clone mã nguồn & tạo Virtual Environment

Khuyến nghị luôn cài đặt và chạy trong môi trường ảo `.venv`:

```bash
# Clone repository
git clone https://github.com/w111user/daily-streak-tool.git
cd daily-streak-tool

# Khởi tạo virtual environment
python3 -m venv .venv

# Kích hoạt virtual environment
source .venv/bin/activate       # Trên Linux/macOS
# hoặc: .venv\Scripts\activate  # Trên Windows
```

### Bước 2: Cài đặt package & các tùy chọn bổ sung (Extras)

Tùy theo nhu cầu sử dụng, chọn lệnh cài đặt tương ứng:

```bash
# Cài đặt Core CLI + Daemon (cơ bản cho server, nhẹ nhất)
pip install -e .

# Cài đặt đầy đủ: Core + TUI giao diện dòng lệnh + Telegram notifications
pip install -e '.[tui,telegram]'

# Cài đặt toàn bộ (bao gồm desktop notifications cho môi trường có màn hình)
pip install -e '.[tui,telegram,desktop]'
```

> **Chi tiết các optional extras trong `pyproject.toml`:**
> - `tui`: Cài đặt `textual>=0.58` để dùng lệnh `streak tui`.
> - `telegram`: Cài đặt `python-telegram-bot>=21.0` để gửi thông báo Telegram.
> - `desktop`: Cài đặt `plyer>=2.1.0` để hiển thị popup thông báo trên desktop.
> - `windows`: Cài đặt `pywin32>=306` (chỉ dành cho hệ điều hành Windows).

### Bước 3: Cài đặt trình duyệt cho Playwright (nếu không dùng Chrome hệ thống)

```bash
playwright install chromium
```

---

## 5. Cấu hình trình duyệt / Browser Setup

Tool tự động tìm kiếm trình duyệt theo thứ tự ưu tiên:
1. Đường dẫn tùy biến trong cấu hình: `tiktok.browser_executable` (nếu khai báo).
2. Trình duyệt hệ thống: `/usr/bin/google-chrome`, `/usr/bin/chromium`, `/usr/bin/chromium-browser`, Chrome trên macOS/Windows.
3. Chromium do Playwright quản lý (`~/.cache/ms-playwright/...`).

Nếu máy chủ Linux dùng đường dẫn Chromium đặc thù, hãy cấu hình đường dẫn tuyệt đối trong `config.json`:

```json
{
  "tiktok": {
    "browser_executable": "/usr/bin/chromium"
  }
}
```

### Chạy giao diện có hiển thị trên Linux Server không có màn hình vật lý

Nếu muốn chạy chế độ có giao diện (`headless: false`) trên máy chủ headless (để tiện giải CAPTCHA hoặc debug), hãy dùng **Xvfb**:

```bash
sudo apt-get install -y xvfb
xvfb-run -a streak daemon
```

---

## 6. Export Cookie TikTok an toàn / Secure Cookie Export

Để tool gửi tin nhắn thay bạn mà không cần nhập mật khẩu mỗi lần, bạn cần export cookie từ trình duyệt đã đăng nhập TikTok:

1. Cài đặt tiện ích mở rộng [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie-v3/ojfebgpkimhlhcblbalbfjblapadhbol) (hoặc Cookie-Editor) trên Chrome.
2. Truy cập [tiktok.com](https://www.tiktok.com) và đăng nhập tài khoản của bạn.
3. Nhấp vào biểu tượng extension -> Chọn **Export** (dữ liệu JSON sẽ được copy vào Clipboard).
4. Tạo file `cookies.json` ở thư mục gốc của project và dán nội dung vào.

> ⚠️ **CẢNH BÁO AN NINH QUAN TRỌNG:**
> - File `cookies.json` và `config.json` chứa phiên đăng nhập và token Telegram cá nhân.
> - **TUYỆT ĐỐI KHÔNG** chia sẻ, gửi, hoặc commit các file này lên GitHub hay bất kỳ kho mã nguồn công khai nào.
> - File `.gitignore` của project đã được cấu hình sẵn để loại trừ `cookies*.json`, `config.json`, và các thư mục auth.

---

## 7. Bảng tham số cấu hình / Configuration Reference

Tạo file `config.json` từ mẫu `config.example`:

```bash
cp config.example config.json
```

### Bảng chi tiết các trường cấu hình trong `config.json`

| Nhóm / Key | Kiểu / Type | Mặc định / Default | Ý nghĩa & Hướng dẫn / Description |
| :--- | :--- | :--- | :--- |
| **`schedule.time`** | `string` | `"09:00"` | Giờ gửi hàng ngày theo định dạng `"HH:MM"` (24 giờ). |
| **`schedule.timezone`** | `string` | `"Asia/Ho_Chi_Minh"` | Múi giờ IANA chuẩn (ví dụ `"UTC"`, `"Asia/Ho_Chi_Minh"`). |
| **`schedule.run_on_start`** | `boolean` | `false` | Nếu `true`, daemon sẽ **gửi ngay 1 lượt** khi vừa khởi động. Khuyến nghị để `false` khi mới cấu hình. |
| **`cookie_file`** | `string` | `"cookies.json"` | Đường dẫn tới file cookie đã export. |
| **`tiktok.headless`** | `boolean` | `false` | `true`: Chạy ngầm không mở cửa sổ browser. `false`: Hiện cửa sổ browser. |
| **`tiktok.persistent_context`** | `boolean` | `true` | `true`: Dùng profile Chrome cố định. `false`: Mở context tạm thời, inject cookies và dọn dẹp sạch sẽ sau khi gửi xong (khuyên dùng cho server headless). |
| **`tiktok.low_memory_mode`** | `boolean` | `false` | `true`: Chặn tải hình ảnh, font, media để tiết kiệm RAM trên VPS/Server yếu. |
| **`tiktok.low_memory_stabilization_seconds`**| `float` | `4.0` | Thời gian chờ DOM ổn định trước khi probe DM trong chế độ low-memory. |
| **`tiktok.browser_executable`** | `string` | `null` | Đường dẫn tuyệt đối tới file thực thi Chrome/Chromium (tùy chọn). |
| **`tiktok.message_delay_seconds`** | `[int, int]` | `[8, 18]` | Khoảng thời gian nghỉ ngẫu nhiên (giây) giữa 2 người nhận liên tiếp để tránh spam detection. |
| **`tiktok.navigation_timeout_ms`** | `int` | `60000` | Timeout điều hướng trang (milliseconds). |
| **`tiktok.dm_readiness_timeout_ms`** | `int` | `45000` | Timeout chờ giao diện danh sách tin nhắn TikTok sẵn sàng. |
| **`tiktok.dm_load_attempts`** | `int` | `2` | Số lần thử tải lại trang `/messages` nếu gặp lỗi timeout. |
| **`tiktok.probe_timeout_ms`** | `int` | `750` | Timeout của các query kiểm tra CAPTCHA / popup. |
| **`tiktok.recipient_retries`** | `int` | `1` | Số lần thử lại tối đa cho mỗi người nhận nếu gặp sự cố trước khi gửi tin nhắn. |
| **`runtime.run_lock_stale_seconds`** | `int` | `21600` (6h) | Thời gian coi lock file là stale nếu tiến trình cũ bị crash đột ngột. |
| **`data_dir`** | `string` | Platform default | Thư mục lưu daily state, history, lock và logs (có thể ghi đè qua biến môi trường `DAILY_STREAK_DATA_DIR`). |
| **`recipients`** | `list[object]` | `[]` | Danh sách người nhận: `[{"name": "Tên bạn bè", "username": "username_tiktok"}]`. |
| **`videos`** | `list[string]` | `[]` | Danh sách link video TikTok dùng để chia sẻ ngẫu nhiên. |
| **`telegram.enabled`** | `boolean` | `false` | Bật/tắt thông báo Telegram. |
| **`telegram.bot_token`** | `string` | `""` | Bot token nhận từ `@BotFather`. |
| **`telegram.chat_id`** | `string` | `""` | ID chat hoặc ID nhóm nhận thông báo. |

---

## 8. Hướng dẫn bắt đầu nhanh / Quick Start

Thực hiện theo các bước chuẩn sau đây để thiết lập và vận hành an toàn:

### Bước 1: Kiểm tra chẩn đoán hệ thống (`doctor`)

Kiểm tra virtual environment, phiên bản Python, trình duyệt, quyền truy cập thư mục dữ liệu và cú pháp config:

```bash
./.venv/bin/streak --config config.json doctor
```

Nếu tất cả mục hiển thị `[PASS]`, môi trường của bạn đã sẵn sàng.

### Bước 2: Kiểm tra trạng thái cấu hình (`status`)

```bash
./.venv/bin/streak --config config.json status
```

Lệnh này hiển thị số lượng người nhận, số video, chế độ browser, lịch trình và thời gian dự kiến gửi tiếp theo mà **không bật browser**.

### Bước 3: Quét và Resolve người nhận (Tùy chọn)

Để lấy danh sách cuộc trò chuyện gần nhất từ tài khoản TikTok của bạn:

```bash
# Quét danh sách DM hiện tại
./.venv/bin/streak --config config.json recipients scan

# Resolve display name sang username chính xác
./.venv/bin/streak --config config.json recipients resolve
```

> *Lưu ý:* Các lệnh trên chỉ in kết quả ra terminal, không tự ý sửa đổi `config.json`. Bạn có thể sao chép kết quả vào trường `recipients` của `config.json` hoặc dùng giao diện TUI để chọn trực quan.

### Bước 4: Mở giao diện TUI tương tác

```bash
./.venv/bin/streak --config config.json tui
```

Trong TUI:
- Nhấn `Space` để chọn/bỏ chọn người nhận bạn muốn gửi.
- Xem tab **Daemon** để kiểm tra lịch gửi.
- Nhấn `s` để gửi thử 1 lượt (chế độ bình thường), hoặc xem mục tiếp theo.

### Bước 5: Gửi thử nghiệm an toàn lần đầu

Để kiểm tra gửi tin nhắn đến các người nhận đã chọn:

```bash
# Chạy gửi bình thường (chỉ gửi những ai chưa gửi hôm nay)
./.venv/bin/streak --config config.json send
```

Quan sát log để kiểm tra kết quả xác thực giao hàng (`VERIFIED`).

---

## 9. Tra cứu lệnh CLI / CLI Command Reference

Cú pháp tổng quát:

```bash
streak [--config <path>] [--verbose] <command> [options]
```

> **Lưu ý về vị trí cờ `--config`:** Cờ `--config` là tùy chọn toàn cục (global flag), cần đặt **trước** tên lệnh con. Ví dụ: `streak --config ./my_config.json status`.

### Danh sách lệnh:

- **`streak doctor [--json]`**:
  Kiểm tra toàn bộ điều kiện chạy offline: Python, Playwright, Chrome binary, thư mục dữ liệu, cookie file, cấu trúc `config.json`.
- **`streak status [--json]`**:
  In tóm tắt trạng thái: số lượng recipients, số video, tiến độ gửi hôm nay, trạng thái daemon, và thời gian chạy tiếp theo.
- **`streak history [--limit N] [--json]`**:
  Xem lại lịch sử các lần chạy gần nhất (mặc định 10 lượt gần nhất) kèm số lượng Success, Failed, Unconfirmed, Skipped.
- **`streak recipients list [--json]`**:
  Liệt kê danh sách người nhận đang có trong `config.json` (không mở trình duyệt).
- **`streak recipients scan [--json]`**:
  Mở browser phiên tạm để quét các cuộc trò chuyện DM có trên tài khoản TikTok.
- **`streak recipients resolve [--json]`**:
  Mở browser phiên tạm để tìm username tương ứng với các display name đã cấu hình.
- **`streak videos list [--json]`**:
  Liệt kê danh sách URL video đang có trong pool.
- **`streak send [--force] [--json]`**:
  - `streak send`: Gửi video cho các người nhận đã chọn. Tự động bỏ qua người đã nhận thành công trong ngày hôm nay.
  - `streak send --force`: **Gửi cưỡng bức (Force Send)** cho tất cả người nhận, bất kể hôm nay đã gửi thành công hay chưa. *(Dùng cẩn thận để tránh gửi lặp video trong ngày)*.
- **`streak tui`**:
  Mở giao diện Textual Terminal Dashboard trực quan.
- **`streak daemon [--status] [--stop] [--json]`**:
  - `streak daemon`: Chạy daemon lập lịch ở chế độ foreground.
  - `streak daemon --status`: Kiểm tra daemon đang chạy hay đã dừng (kèm PID nếu có).
  - `streak daemon --stop`: Gửi tín hiệu dừng an toàn tới daemon đang chạy ngầm.

---

## 10. Giao diện dòng lệnh TUI / Textual TUI Reference

Khởi chạy bằng lệnh:

```bash
streak --config config.json tui
```

### Các Tab chức năng:
1. **Dashboard**: Hiển thị tổng quan cấu hình, múi giờ, số người hoàn thành hôm nay, trạng thái browser và kết quả đợt gửi vừa qua.
2. **Results**: Bảng chi tiết kết quả từng người nhận (Trạng thái, số lần thử, video đã chọn, chi tiết lỗi nếu có).
3. **Recipients**: Danh sách người nhận kèm checkbox chọn/bỏ chọn trực tiếp. Thao tác chọn được lưu ngay lập tức vào `config.json`.
4. **Videos**: Danh sách video trong pool.
5. **History**: Bảng nhật ký lịch sử các lần chạy trước.
6. **Daemon**: Quản lý daemon ngầm: trạng thái PID, lịch gửi tiếp theo, nút Start / Stop / Refresh daemon, và khung xem 100 dòng log daemon gần nhất.

### Phím tắt (Keyboard Shortcuts):

| Phím / Key | Chức năng / Action |
| :---: | :--- |
| `s` | **Send**: Bắt đầu gửi với cơ chế daily resume (bỏ qua người đã gửi hôm nay). |
| `f` | **Force Send**: Bắt đầu gửi cưỡng bức cho toàn bộ người nhận đang chọn. |
| `c` | **Scan**: Mở browser quét danh sách cuộc trò chuyện TikTok DM. |
| `r` | **Resolve**: Tìm kiếm username cho các người nhận được chọn. |
| `Space` | **Toggle**: Chọn / Bỏ chọn người nhận đang trỏ tới. |
| `a` | **Select All**: Chọn tất cả người nhận trong danh sách. |
| `d` | **Deselect All**: Bỏ chọn tất cả người nhận. |
| `u` | **Start Daemon**: Khởi chạy tiến trình daemon chạy ngầm độc lập. |
| `k` | **Stop Daemon**: Dừng an toàn tiến trình daemon chạy ngầm. |
| `g` | **Refresh**: Nạp lại cấu hình, lịch sử và trạng thái daemon từ đĩa. |
| `q` | **Quit**: Thoát TUI (nếu daemon đang chạy ngầm, daemon vẫn tiếp tục hoạt động). |

---

## 11. Vận hành Daemon / Daemon Lifecycle & Supervision

Daemon chịu trách nhiệm chạy các đợt gửi tự động theo giờ đã hẹn trong `schedule.time`.

### Hai cách vận hành Daemon:

1. **Chạy Foreground (CLI)**:
   Phù hợp khi chạy dưới `systemd`, `Docker`, hoặc trong phiên `tmux`/`screen`:
   ```bash
   streak --config config.json daemon
   ```
   Nhấn `Ctrl+C` (SIGINT) hoặc gửi `SIGTERM` để daemon hoàn thành an toàn và thoát.

2. **Chạy Detached (Khởi động từ TUI)**:
   Trong TUI tab **Daemon**, nhấn **Start Daemon** (hoặc bấm phím `u`).
   - Daemon được tách thành tiến trình ngầm độc lập (detached background process).
   - Khi bạn thoát TUI (`q`), **daemon vẫn tiếp tục chạy bình thường**.
   - Khi mở lại TUI vào lần sau, tab Daemon sẽ tự động nhận diện lại PID và hiển thị log mới nhất.
   - Để tắt daemon ngầm, bấm **Stop Daemon** (phím `k`) trong TUI hoặc chạy `streak daemon --stop` từ terminal.

### Cơ chế khóa an toàn (Dual Locks):
- **`daemon.lock`**: Khóa vòng đời của tiến trình daemon, ghi nhận PID và hostname, ngăn việc khởi chạy trùng lặp nhiều daemon cùng lúc trên một thư mục dữ liệu.
- **`send.lock`**: Khóa thực thi phiên gửi tin. Khi daemon hoặc người dùng đang gửi tin nhắn, bất kỳ lệnh `send` nào khác phát sinh đồng thời sẽ bị từ chối ngay lập tức để tránh tranh chấp trình duyệt.

### Vấn đề khởi động lại máy chủ (Reboot Persistence):
> ℹ️ **LƯU Ý:** Tiến trình daemon chạy ngầm **không thể tự khởi động lại sau khi reboot máy chủ** nếu không có trình quản lý tiến trình.
> Trên môi trường production, hãy cấu hình một dịch vụ `systemd` đơn giản:

```ini
# /etc/systemd/system/daily-streak.service
[Unit]
Description=Daily Streak Tool Daemon
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/daily-streak-tool
ExecStart=/path/to/daily-streak-tool/.venv/bin/streak --config config.json daemon
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Kích hoạt service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daily-streak.service
```

---

## 12. Cơ chế xác thực gửi tin / Delivery Verification

Trong các phiên bản trước đây, việc nhấn phím `Enter` hoặc làm sạch ô soạn tin thường được mặc định là gửi thành công. Trong thực tế mạng chậm hoặc giao diện TikTok bị lag, tin nhắn có thể chưa được gửi đi.

Phiên bản v2 áp dụng **Bounded Outgoing Delivery Verification**:
Sau khi submit tin nhắn, hệ thống theo dõi danh sách DOM trò chuyện trong một khoảng thời gian giới hạn để tìm bằng chứng rõ ràng:
1. Quan sát thấy tin nhắn mới được thêm vào danh sách chat.
2. Kiểm tra phần tử thuộc về **tin nhắn gửi đi của chính bạn** (không phải tin nhắn đối phương vừa gửi tới).
3. Nội dung bong bóng chat chứa đúng URL video hoặc Video ID trong pool đã chọn.

### Ý nghĩa 4 trạng thái kết quả (`RecipientStatus`):

| Trạng thái | Định nghĩa | Hành vi hệ thống |
| :--- | :--- | :--- |
| **`SUCCESS`** | Đã xác thực giao hàng thành công (Verified Sent). | Ghi nhận hoàn thành vào `daily_state.json` và lưu vào lịch sử. Lần chạy sau trong ngày sẽ bỏ qua. |
| **`FAILED`** | Lỗi xác định trước khi submit (không tìm thấy composer, chat bị khóa, v.v.) sau khi đã hết số lần retry. | Ghi nhận thất bại; **vẫn đủ điều kiện** để thử lại trong lần chạy tiếp theo. |
| **`UNCONFIRMED`** | Phím Enter đã được bấm, nhưng trong thời gian chờ không tìm thấy bong bóng tin nhắn xuất hiện trong DOM. | **Không tự động resend** ngay trong đợt để tránh nguy cơ gửi lặp 2 lần; đồng thời **không ghi nhận success** vào daily state để bạn nắm được tình trạng. |
| **`SKIPPED`** | Người nhận đã được ghi nhận `SUCCESS` trước đó trong cùng ngày theo múi giờ chỉ định. | Bỏ qua để tiết kiệm thời gian và tránh làm phiền bạn bè. |

---

## 13. Thông báo Telegram & Desktop / Notifications

Khi cài đặt extra `.[telegram]`, bạn có thể nhận thông báo tức thời về tình trạng hoạt động của công cụ:

```bash
pip install -e '.[telegram]'
```

### Các sự kiện gửi thông báo:
1. **Yêu cầu đăng nhập lại**: Khi cookie hết hạn hoặc phiên đăng nhập TikTok bị vô hiệu hóa.
2. **Cảnh báo CAPTCHA**: Khi phát hiện màn hình giải đố trượt hoặc xoay ảnh (kèm URL trang để bạn can thiệp).
3. **Tổng kết hoàn thành Batch (Batch-Completion Summary)**:
   Được tự động gửi ngay sau khi kết thúc:
   - Một đợt gửi theo lịch trình của Daemon.
   - Một đợt gửi thủ công qua TUI Send (`s`) hoặc Force Send (`f`).

**Mẫu tin nhắn Telegram:**
```text
Daily Streak Tool — batch completed
Success: 18
Failed: 0
Unconfirmed: 0
Skipped: 0
```

> **Cách ly lỗi (Fault-Tolerant):** Mọi lỗi liên quan đến mạng Telegram, sai bot token hoặc timeout đều được bắt và ghi log riêng. Lỗi gửi Telegram **tuyệt đối không làm thay đổi kết quả batch TikTok** hay gây crash tiến trình.

---

## 14. Quản lý trạng thái & Lịch sử / State & History

Để đảm bảo an toàn và không làm bẩn thư mục mã nguồn, toàn bộ dữ liệu trạng thái được lưu mặc định tại thư mục chuẩn của hệ điều hành:

- **Linux**: `~/.local/state/daily-streak-tool/` (hoặc `$XDG_STATE_HOME/daily-streak-tool/`)
- **macOS**: `~/Library/Application Support/daily-streak-tool/`
- **Windows**: `%LOCALAPPDATA%\daily-streak-tool\`

### Các file lưu trữ:
- `daily_state.json`: Lưu danh sách các username đã nhận video thành công theo ngày (ví dụ `2026-10-15`). Dữ liệu được ghi theo cơ chế atomic write (ghi file tạm rồi đổi tên) để chống hỏng file khi mất điện đột ngột.
- `history.jsonl`: Lưu nhật ký các đợt chạy dưới dạng JSON Lines (mỗi đợt 1 dòng).
- `send.lock` & `daemon.lock`: Các file khóa quản lý tiến trình.
- `daemon.log`: File ghi log của daemon khi chạy detached.

> **Reset trạng thái:** Nếu muốn xóa dữ liệu đã gửi hôm nay để chạy lại từ đầu mà không cần dùng `--force`, bạn chỉ cần xóa file `daily_state.json` trong thư mục dữ liệu nói trên.

---

## 15. Phát triển & Kiểm thử / Development & Testing

Dự án tuân thủ tiêu chuẩn kiểm thử tự động nghiêm ngặt bằng `unittest` chuẩn của Python. Toàn bộ test suite sử dụng mocks/fakes, **không bao giờ mở kết nối mạng thật hoặc tự động hóa tài khoản TikTok thật trong quá trình test**.

Chạy bộ kiểm tra toàn diện:

```bash
# 1. Kiểm tra biên dịch bytecode toàn bộ project
./.venv/bin/python -m compileall -q .

# 2. Chạy toàn bộ 158 automated unit tests
./.venv/bin/python -m unittest discover -v

# 3. Kiểm tra định dạng git diff và khoảng trắng thừa
git diff --check
```

---

## 16. Xử lý sự cố & Bảo mật / Troubleshooting & Security

### 1. Lỗi "Cookies expired or invalid"
- Cookie TikTok thường hết hạn sau 1 - 3 tháng hoặc khi bạn nhấn Đăng xuất trên trình duyệt máy tính.
- **Cách khắc phục:** Đăng nhập lại TikTok trên Chrome và xuất lại file `cookies.json`.

### 2. Gặp CAPTCHA của TikTok
- TikTok thỉnh thoảng kích hoạt CAPTCHA xác minh người dùng (nhất là khi đổi IP mới hoặc gửi quá nhanh).
- Khi phát hiện CAPTCHA, tool sẽ gửi cảnh báo Telegram/Desktop và tạm dừng chờ bạn giải. Hãy mở trình duyệt và hoàn thành giải đố, tool sẽ tự động tiếp tục sau khi CAPTCHA biến mất.

### 3. Trình duyệt không khởi động được trên Linux Server
- Hãy đảm bảo đã cài đặt đủ các thư viện phụ thuộc của Chromium:
  ```bash
  playwright install-deps chromium
  ```
- Hoặc cấu hình chạy với Xvfb nếu chạy chế độ có giao diện.

### 4. Xóa stale lock file khi tiến trình bị tắt cưỡng bức
- Nếu máy chủ bị sập nguồn đột ngột trong lúc đang gửi tin, file `send.lock` có thể vẫn còn. Tool có cơ chế tự động dọn stale lock sau thời gian cấu hình (`run_lock_stale_seconds`), hoặc bạn có thể xóa file `send.lock` trong thư mục dữ liệu để tiếp tục ngay.

---

## 17. Điều khoản sử dụng / Disclaimer

> ⚠️ **TUYÊN BỐ MIỄN TRỪ TRÁCH NHIỆM / DISCLAIMER:**
>
> 1. Dự án này được phát triển phục vụ mục đích **nghiên cứu kỹ thuật, học tập và tự động hóa cá nhân**.
> 2. Việc tự động hóa tương tác trên nền tảng TikTok có thể vi phạm [Điều khoản Dịch vụ của TikTok (TikTok Terms of Service)](https://www.tiktok.com/legal/page/row/terms-of-service/en).
> 3. Tác giả và những người đóng góp cho dự án này **không chịu trách nhiệm** về bất kỳ hình thức xử lý tài khoản nào (bao gồm cảnh cáo, giới hạn tính năng direct message, hoặc khóa tài khoản) phát sinh từ việc sử dụng công cụ này.
> 4. Hãy sử dụng công cụ một cách có trách nhiệm: cấu hình khoảng thời gian delay hợp lý (`message_delay_seconds`), không spam link độc hại, và chỉ gửi video cho bạn bè quen biết.
