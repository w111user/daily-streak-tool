"""Independent Telegram Remote Controller for Daily Streak Tool v2."""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Mapping
import uuid
from zoneinfo import ZoneInfo

from daily_streak.config import DEFAULT_CONFIG_PATH, load_config
from daily_streak.daemon import (
    DaemonManager,
    DaemonService,
    ProcessDaemonManager,
    SystemdDaemonManager,
    create_daemon_service,
    create_sender,
)
from daily_streak.history import RunHistoryStore
from daily_streak.lock import DaemonLock, RunLock
from daily_streak.models import RunSummary
from daily_streak.paths import get_data_dir
from daily_streak.schedule import next_run, schedule_from_config
from daily_streak.state import DailyStateStore, PauseStore


logger = logging.getLogger(__name__)

CONFIRMATION_EXPIRATION_SECONDS = 60.0
ConfigLoader = Callable[[str | Path], dict[str, Any]]
SenderFactory = Callable[[Mapping[str, Any]], Any]


class TelegramExtraRequiredError(RuntimeError):
    """Raised when python-telegram-bot is required but not installed."""


@dataclass(slots=True)
class RunConfirmation:
    token: str
    chat_id: str
    created_at: float
    expires_at: float
    consumed: bool = False


class TelegramController:
    """Manages the long-polling Telegram bot remote controller."""

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        account_name: str | None = None,
        config_loader: ConfigLoader = load_config,
        daemon_service: DaemonService | None = None,
        sender_factory: SenderFactory = create_sender,
        now_provider: Callable[[], datetime] | None = None,
        streak_refresher: Callable[..., Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config_loader = config_loader
        self.sender_factory = sender_factory
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._daemon_service = daemon_service
        self._confirmations: dict[str, RunConfirmation] = {}
        self._sender_lock = asyncio.Lock()
        self._streaks_refresh_lock = asyncio.Lock()
        self._streak_refresher = streak_refresher

        initial_config = self.config_loader(self.config_path)

        resolved_account = str(account_name).strip() if account_name else None
        if not resolved_account:
            raw_acc = initial_config.get("account_name") or initial_config.get("account")
            if raw_acc:
                resolved_account = str(raw_acc).strip()
            elif (
                len(self.config_path.parts) >= 3
                and self.config_path.parts[-3] == "accounts"
                and self.config_path.name.endswith(".json")
            ):
                resolved_account = self.config_path.parent.name
            elif (
                len(self.config_path.parts) >= 2
                and self.config_path.parts[-2] == "accounts"
                and self.config_path.suffix == ".json"
            ):
                resolved_account = self.config_path.stem
        self.account_name = resolved_account if resolved_account and resolved_account != "default" else None

        raw_telegram = initial_config.get("telegram", {})
        telegram_cfg: Mapping[str, Any] = (
            raw_telegram if isinstance(raw_telegram, Mapping) else {}
        )
        self.bot_token = str(telegram_cfg.get("bot_token", "")).strip()
        self.configured_chat_id = str(telegram_cfg.get("chat_id", "")).strip()
        self.commands_enabled = bool(telegram_cfg.get("commands_enabled", False))

    def _get_config(self) -> dict[str, Any]:
        cfg = dict(self.config_loader(self.config_path))
        if self.account_name and "account_name" not in cfg:
            cfg["account_name"] = self.account_name
        return cfg

    def get_daemon_service(self) -> DaemonService:
        if self._daemon_service is not None:
            return self._daemon_service
        config = self._get_config()
        return create_daemon_service(
            config,
            self.config_path,
            account_name=self.account_name,
        )

    def is_authorized(self, update: Any) -> bool:
        """Verify update originated from the authorized chat_id."""
        effective_chat = getattr(update, "effective_chat", None)
        if effective_chat is None:
            return False
        chat_id_str = str(getattr(effective_chat, "id", "")).strip()
        if not self.configured_chat_id or chat_id_str != self.configured_chat_id:
            logger.warning(
                "Ignored update from unauthorized chat ID %s (authorized: %s)",
                chat_id_str,
                self.configured_chat_id,
            )
            return False
        return True

    def _cleanup_expired_confirmations(self) -> None:
        now = time.time()
        expired = [token for token, conf in self._confirmations.items() if now > conf.expires_at]
        for token in expired:
            self._confirmations.pop(token, None)

    async def handle_help(self, update: Any, context: Any) -> None:
        """Handle /help or /start initial greeting."""
        if not self.is_authorized(update):
            return

        help_text = (
            "🤖 *Daily Streak Tool — Telegram Remote Controller*\n\n"
            "Danh sách lệnh khả dụng:\n"
            "• `/help` — Xem hướng dẫn và danh sách lệnh\n"
            "• `/status` — Trạng thái chi tiết (daemon, recipient, lịch gửi)\n"
            "• `/last` — Kết quả lượt gửi gần nhất từ lịch sử\n"
            "• `/next` — Lịch gửi tự động kế tiếp\n"
            "• `/streaks` — Xem và làm mới trạng thái streak TikTok\n"
            "• `/start` — Khởi động daemon scheduler\n"
            "• `/stop` — Dừng daemon scheduler\n"
            "• `/pause` — Tạm dừng gửi tự động (giữ nguyên daemon)\n"
            "• `/resume` — Tiếp tục gửi tự động (hủy tạm dừng)\n"
            "• `/run` — Kích hoạt một lượt gửi thủ công ngay (xác nhận Inline)"
        )
        if update.message:

            await update.message.reply_text(help_text, parse_mode="Markdown")

    async def handle_status(self, update: Any, context: Any) -> None:
        """Handle /status command."""
        if not self.is_authorized(update):
            return

        config = self._get_config()
        data_dir = get_data_dir(config, account_name=self.account_name)
        service = self.get_daemon_service()
        status_info = service.get_status()

        schedule = schedule_from_config(config)
        now_dt = self.now_provider()
        local_now = (
            now_dt.astimezone(schedule.timezone)
            if now_dt.tzinfo
            else now_dt.replace(tzinfo=timezone.utc).astimezone(schedule.timezone)
        )
        today_date = local_now.date().isoformat()
        daily_state = DailyStateStore(data_dir).load(today_date, str(schedule.timezone))

        from daily_streak.cli import configured_recipients, configured_videos

        recipients = configured_recipients(config)
        total_recipients = len(recipients)
        completed_recipients = sum(
            1
            for r in recipients
            if isinstance(r.get("username"), str) and daily_state.is_completed(r["username"])
        )

        videos = configured_videos(config)
        video_count = len(videos)

        run_lock = RunLock(data_dir)
        send_lock_active = run_lock.is_active()

        daemon_status = status_info.get("status", "Unknown")
        pid = status_info.get("pid")
        pid_text = f" (PID: {pid})" if pid else ""
        backend = status_info.get("backend", "process")
        account_line = f"• *Tài khoản:* `{self.account_name}`\n" if self.account_name else ""

        last_run = status_info.get("last_run")
        if not last_run:
            records = RunHistoryStore(data_dir).load(limit=1)
            last_run = records[-1] if records else None

        last_batch_line = ""
        if isinstance(last_run, dict):
            finished_at = (
                last_run.get("finished_at")
                or last_run.get("completed_at")
                or last_run.get("started_at")
            )
            if finished_at:
                success = last_run.get("success", last_run.get("success_count", 0))
                failed = last_run.get("failed", last_run.get("failed_count", 0))
                last_batch_line = f"\n• *Lượt gần nhất:* `{finished_at}` (thành công: {success}, thất bại: {failed})"

        text = (
            "📊 *Trạng thái Daily Streak Tool*\n\n"
            f"{account_line}"
            f"• *Daemon scheduler:* `{daemon_status}`{pid_text}\n"
            f"• *Backend:* `{backend}`\n"
            f"• *Tiến độ hôm nay ({today_date}):* `{completed_recipients}/{total_recipients}` tài khoản hoàn tất\n"
            f"• *Kho video:* `{video_count}` video cấu hình\n"
            f"• *Lịch tự động:* `{schedule.time_text}` ({schedule.timezone})\n"
            f"• *Lượt tiếp theo:* `{status_info.get('next_run', 'N/A')}`\n"
            f"• *Khóa gửi (`send.lock`):* `{'Đang hoạt động' if send_lock_active else 'Không hoạt động'}`"
            f"{last_batch_line}"
        )
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_last(self, update: Any, context: Any) -> None:
        """Handle /last command showing the latest run record."""
        if not self.is_authorized(update):
            return

        config = self._get_config()
        data_dir = get_data_dir(config, account_name=self.account_name)
        history_store = RunHistoryStore(data_dir)
        records = history_store.load(limit=1)

        if not records:
            if update.message:
                await update.message.reply_text("Chưa có dữ liệu lịch sử chạy nào được ghi nhận.")
            return

        last = records[-1]
        completed_at = (
            last.get("finished_at")
            or last.get("completed_at")
            or last.get("started_at")
            or "N/A"
        )
        duration = last.get("elapsed_seconds")
        if duration is None:
            duration = last.get("duration_seconds")
        duration_text = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "N/A"

        success = last.get("success", last.get("success_count", 0))
        failed = last.get("failed", last.get("failed_count", 0))
        unconfirmed = last.get("unconfirmed", last.get("unconfirmed_count", 0))
        skipped = last.get("skipped", last.get("skipped_count", 0))
        trigger = last.get("trigger", "batch")
        setup_error = last.get("setup_error")


        text = (
            "📋 *Kết quả lượt gửi gần nhất*\n\n"
            f"• *Thời điểm:* `{completed_at}`\n"
            f"• *Loại chạy:* `{trigger}`\n"
            f"• *Thời gian thực thi:* `{duration_text}`\n"
            f"• *Thành công:* `{success}`\n"
            f"• *Thất bại:* `{failed}`\n"
            f"• *Chưa xác nhận:* `{unconfirmed}`\n"
            f"• *Bỏ qua:* `{skipped}`"
        )
        if setup_error:
            text += f"\n• *Lỗi khởi chạy:* `{setup_error}`"

        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_next(self, update: Any, context: Any) -> None:
        """Handle /next command calculating next scheduled batch."""
        if not self.is_authorized(update):
            return

        config = self._get_config()
        service = self.get_daemon_service()
        is_paused = service.is_paused()

        schedule = schedule_from_config(config)
        now_dt = self.now_provider()
        local_now = (
            now_dt.astimezone(schedule.timezone)
            if now_dt.tzinfo
            else now_dt.replace(tzinfo=timezone.utc).astimezone(schedule.timezone)
        )
        next_run_dt = next_run(schedule, local_now)
        diff = next_run_dt - local_now
        total_seconds = max(0, int(diff.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        countdown = f"{hours} giờ {minutes} phút" if hours > 0 else f"{minutes} phút"

        paused_notice = ""
        if is_paused:
            paused_notice = "\n\n⚠️ *Lưu ý:* Daemon scheduler đang ở trạng thái *Tạm dừng* (`/pause`). Lượt chạy này sẽ bị bỏ qua trừ khi bạn gõ `/resume`."

        text = (
            "⏰ *Lịch gửi tự động kế tiếp*\n\n"
            f"• *Thời gian:* `{next_run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}`\n"
            f"• *Đếm ngược:* còn `{countdown}`"
            f"{paused_notice}"
        )
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

    def _build_streaks_keyboard(self) -> Any:
        """Build the inline keyboard with '↻ Làm mới streak' button."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            class InlineKeyboardButton:  # type: ignore[no-redef]
                def __init__(self, text: str, callback_data: str) -> None:
                    self.text = text
                    self.callback_data = callback_data

            class InlineKeyboardMarkup:  # type: ignore[no-redef]
                def __init__(self, inline_keyboard: list[Any]) -> None:
                    self.inline_keyboard = inline_keyboard

        keyboard = [
            [InlineKeyboardButton("↻ Làm mới streak", callback_data="streaks_refresh")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def _format_streaks_message(
        self,
        recipients: Sequence[Mapping[str, Any]],
        cache: Any,
        *,
        error_notice: str | None = None,
    ) -> str:
        """Format streak status for recipients including last refresh timestamp and status markers."""
        from daily_streak.streak_reader import format_streak_display

        lines = ["🔥 *Trạng thái Streak TikTok*\n"]
        latest_success_dt: datetime | None = None

        for r in recipients:
            if not isinstance(r, Mapping):
                continue
            name = str(r.get("name") or "")
            username = str(r.get("username") or "")
            record = cache.get(username) if username else None
            disp = format_streak_display(record)
            target = f"{name} (@{username})" if username else f"{name} (unresolved)"
            time_tag = ""
            if record and record.updated_at:
                try:
                    dt = datetime.fromisoformat(record.updated_at)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if latest_success_dt is None or dt > latest_success_dt:
                        latest_success_dt = dt
                    time_tag = f" `({dt.strftime('%H:%M %d/%m')})`"
                except Exception:
                    pass
            lines.append(f"• *{target}*: {disp}{time_tag}")

        lines.append("")
        if latest_success_dt is not None:
            time_str = latest_success_dt.strftime("%H:%M:%S %d/%m/%Y")
            lines.append(f"• *Lần làm mới gần nhất:* `{time_str}`")
        else:
            lines.append("• *Lần làm mới gần nhất:* `Chưa có dữ liệu`")

        if error_notice:
            lines.append(f"\n⚠️ *Lỗi làm mới:* {error_notice}\n_(Dữ liệu hiển thị phía trên vẫn là bản cache gần nhất)_")
        else:
            lines.append("\n_Nhấn nút bên dưới để làm mới trực tiếp từ TikTok API._")

        return "\n".join(lines)

    @staticmethod
    def _sanitize_streak_error(exc: Exception) -> str:
        """Produce a safe, operator-friendly error message without leaking secrets."""
        from daily_streak.lock import StreakLockedError

        if isinstance(exc, StreakLockedError):
            return "Một tiến trình khác đang cập nhật streak (`streak.lock`). Vui lòng thử lại sau."

        err_str = str(exc).lower()
        if "401" in err_str or "login" in err_str or "cookie" in err_str or "auth" in err_str:
            return "Phiên đăng nhập TikTok (cookie) không hợp lệ hoặc đã hết hạn."
        if "429" in err_str or "rate limit" in err_str or "too many" in err_str:
            return "TikTok giới hạn tần suất yêu cầu (rate limit). Vui lòng đợi một lát."
        if "timeout" in err_str or "connect" in err_str or "network" in err_str:
            return "Lỗi kết nối mạng khi tải dữ liệu từ TikTok."
        if "protobuf" in err_str or "decode" in err_str or "parse" in err_str:
            return "Không thể phân tích dữ liệu phản hồi từ TikTok."
        return f"Lỗi không xác định ({type(exc).__name__})."

    async def handle_streaks(self, update: Any, context: Any) -> None:
        """Handle /streaks command (cached streak status with interactive refresh)."""
        if not self.is_authorized(update):
            return

        config = self._get_config()
        data_dir = get_data_dir(config, account_name=self.account_name)
        from daily_streak.streak_reader import StreakCache

        cache = StreakCache(data_dir)
        recipients = config.get("recipients", [])
        if not isinstance(recipients, list) or not recipients:
            if update.message:
                await update.message.reply_text("Không có recipient nào được cấu hình.")
            return

        text = self._format_streaks_message(recipients, cache)
        reply_markup = self._build_streaks_keyboard()
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def handle_start(self, update: Any, context: Any) -> None:

        """Handle /start command to start the background daemon."""
        if not self.is_authorized(update):
            return

        service = self.get_daemon_service()
        status = service.get_status()
        if status.get("is_running"):
            pid = status.get("pid")
            pid_text = f" (PID: {pid})" if pid else ""
            if update.message:
                await update.message.reply_text(f"ℹ️ Daemon đã đang hoạt động{pid_text}.")
            return

        try:
            pid = service.start()
            pid_text = f" (PID: {pid})" if pid else ""
            if update.message:
                await update.message.reply_text(f"✅ Đã khởi động daemon scheduler thành công{pid_text}.")
        except Exception as exc:
            logger.exception("Failed to start daemon via Telegram")
            if update.message:
                await update.message.reply_text(f"❌ Không thể khởi động daemon: {exc}")

    async def handle_stop(self, update: Any, context: Any) -> None:
        """Handle /stop command to stop the background daemon."""
        if not self.is_authorized(update):
            return

        service = self.get_daemon_service()
        status = service.get_status()
        if not status.get("is_running"):
            if update.message:
                await update.message.reply_text("ℹ️ Daemon hiện đang không hoạt động.")
            return

        try:
            service.stop()
            if update.message:
                await update.message.reply_text("🛑 Đã dừng daemon scheduler.")
        except Exception as exc:
            logger.exception("Failed to stop daemon via Telegram")
            if update.message:
                await update.message.reply_text(f"❌ Lỗi khi dừng daemon: {exc}")

    async def handle_pause(self, update: Any, context: Any) -> None:
        """Handle /pause command to pause daemon scheduled batches."""
        if not self.is_authorized(update):
            return

        service = self.get_daemon_service()
        service.pause(reason="telegram")
        text = (
            "⏸ *Đã tạm dừng các lượt gửi tự động.*\n\n"
            "Các lượt chạy theo lịch trình sẽ tự động bị bỏ qua cho đến khi bạn gõ `/resume`.\n"
            "Tiến trình daemon vẫn duy trì hoạt động."
        )
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_resume(self, update: Any, context: Any) -> None:
        """Handle /resume command to clear pause state."""
        if not self.is_authorized(update):
            return

        service = self.get_daemon_service()
        was_paused = service.resume()
        status = service.get_status()
        is_running = status.get("is_running")

        text = "▶️ *Đã tiếp tục gửi tự động (hủy tạm dừng).*\n\n"
        if not was_paused:
            text += "Scheduler trước đó không ở trạng thái tạm dừng.\n"
        else:
            text += "Lịch gửi tự động đã được kích hoạt trở lại.\n"

        if not is_running:
            text += "\n⚠️ *Lưu ý:* Daemon scheduler hiện đang dừng. Bạn có thể gõ `/start` để khởi động lại daemon."

        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_run(self, update: Any, context: Any) -> None:
        """Handle /run command: interactive confirmation with 60s expiration."""
        if not self.is_authorized(update):
            return

        config = self._get_config()
        data_dir = get_data_dir(config, account_name=self.account_name)
        run_lock = RunLock(data_dir)

        if run_lock.is_active():
            if update.message:
                await update.message.reply_text(
                    "⚠️ *Không thể chạy lúc này:* Một tiến trình gửi link khác đang hoạt động (`send.lock`).\n"
                    "Vui lòng đợi lượt hiện tại hoàn tất.",
                    parse_mode="Markdown",
                )
            return

        self._cleanup_expired_confirmations()

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            class InlineKeyboardButton:  # type: ignore[no-redef]
                def __init__(self, text: str, callback_data: str) -> None:
                    self.text = text
                    self.callback_data = callback_data

            class InlineKeyboardMarkup:  # type: ignore[no-redef]
                def __init__(self, inline_keyboard: list[Any]) -> None:
                    self.inline_keyboard = inline_keyboard

        token = uuid.uuid4().hex[:12]
        now = time.time()
        self._confirmations[token] = RunConfirmation(
            token=token,
            chat_id=str(update.effective_chat.id),
            created_at=now,
            expires_at=now + CONFIRMATION_EXPIRATION_SECONDS,
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ Xác nhận gửi (Confirm)", callback_data=f"run_confirm:{token}"),
                InlineKeyboardButton("❌ Hủy (Cancel)", callback_data=f"run_cancel:{token}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        prompt_text = (
            "⚠️ *Xác nhận khởi chạy gửi link thủ công*\n\n"
            "Hành động này sẽ mở trình duyệt và gửi video TikTok cho các tài khoản chưa hoàn tất hôm nay.\n\n"
            "⏳ *Yêu cầu này sẽ hết hạn sau 60 giây.*"
        )
        if update.message:
            await update.message.reply_text(prompt_text, reply_markup=reply_markup, parse_mode="Markdown")

    async def handle_callback(self, update: Any, context: Any) -> None:
        """Handle inline button callback queries for /run confirmation and /streaks refresh."""
        query = update.callback_query
        if not query:
            return

        if not self.is_authorized(update):
            await query.answer("Truy cập không được phép.", show_alert=True)
            return

        data = query.data or ""
        if data == "streaks_refresh":
            if self._streaks_refresh_lock.locked():
                await query.answer("Đang trong quá trình làm mới streak, vui lòng đợi...", show_alert=True)
                return

            config = self._get_config()
            data_dir = get_data_dir(config, account_name=self.account_name)
            from daily_streak.lock import StreakLock

            streak_lock = StreakLock(data_dir)
            if streak_lock.is_active():
                await query.answer("Tiến trình làm mới streak khác đang chạy (`streak.lock`)!", show_alert=True)
                return

            await query.answer("Đang làm mới dữ liệu streak từ TikTok...")
            asyncio.create_task(self._execute_streaks_refresh(query))
            return

        if not (data.startswith("run_confirm:") or data.startswith("run_cancel:")):
            await query.answer()
            return

        action, token = data.split(":", 1)
        conf = self._confirmations.get(token)

        now = time.time()
        if not conf or conf.consumed or now > conf.expires_at:
            if conf:
                conf.consumed = True
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            try:
                await query.edit_message_text("⚠️ *Yêu cầu chạy thủ công đã hết hạn hoặc không còn hiệu lực.*", parse_mode="Markdown")
            except Exception:
                pass
            return

        conf.consumed = True
        self._confirmations.pop(token, None)

        if action == "run_cancel":
            await query.answer("Đã hủy.")
            try:
                await query.edit_message_text("❌ *Đã hủy lệnh gửi link TikTok.*", parse_mode="Markdown")
            except Exception:
                pass
            return

        # action == "run_confirm"
        config = self._get_config()
        data_dir = get_data_dir(config, account_name=self.account_name)
        run_lock = RunLock(data_dir)

        if run_lock.is_active():
            await query.answer("Tiến trình gửi đang bận!", show_alert=True)
            try:
                await query.edit_message_text(
                    "⚠️ *Không thể thực hiện:* Một tiến trình gửi link khác đang hoạt động (`send.lock`).",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return

        await query.answer("Đang bắt đầu lượt gửi link...")
        try:
            await query.edit_message_text(
                "⏳ *Đang tiến hành gửi link TikTok hôm nay...*\nKết quả sẽ được cập nhật ngay khi hoàn tất.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

        asyncio.create_task(self._execute_manual_send(query.message.chat_id if query.message else None))

    async def _execute_manual_send(self, chat_id: int | str | None) -> None:
        """Execute the manual send workflow in the background and reply with the summary."""
        target_chat_id = chat_id or self.configured_chat_id
        if self._sender_lock.locked():
            logger.warning("Manual send skipped because another in-process send is active.")
            return

        async with self._sender_lock:
            try:
                config = self._get_config()
                sender = self.sender_factory(config)
                summary: RunSummary = await sender.send_daily_links()

                if summary.setup_error:
                    msg = f"❌ *Lỗi khởi chạy gửi link:*\n`{summary.setup_error}`"
                else:
                    msg = (
                        "✅ *Hoàn tất lượt gửi TikTok thủ công*\n\n"
                        f"• Thành công: `{summary.success_count}`\n"
                        f"• Thất bại: `{summary.failed_count}`\n"
                        f"• Chưa xác nhận: `{summary.unconfirmed_count}`\n"
                        f"• Bỏ qua: `{summary.skipped_count}`"
                    )

                from telegram import Bot

                bot = Bot(token=self.bot_token)
                await bot.send_message(chat_id=target_chat_id, text=msg, parse_mode="Markdown")
            except Exception as exc:
                logger.exception("Error executing manual send from Telegram callback")
                try:
                    from telegram import Bot

                    bot = Bot(token=self.bot_token)
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=f"❌ *Lỗi khi thực hiện gửi link:*\n`{exc}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send error notification back to Telegram")

    async def _execute_streaks_refresh(self, query: Any) -> None:
        """Execute streak refresh in background and edit the callback message."""
        if self._streaks_refresh_lock.locked():
            return

        async with self._streaks_refresh_lock:
            config = self._get_config()
            data_dir = get_data_dir(config, account_name=self.account_name)
            from daily_streak.streak_reader import StreakCache
            from daily_streak.recipient_actions import refresh_streaks

            recipients = config.get("recipients", [])
            cache = StreakCache(data_dir)
            error_notice: str | None = None

            try:
                if self._streak_refresher is not None:
                    try:
                        res = self._streak_refresher(config, recipients, force=True)
                    except TypeError:
                        res = self._streak_refresher(config, recipients)
                    if asyncio.iscoroutine(res):
                        await res
                else:
                    await refresh_streaks(config, recipients, force=True)
                cache = StreakCache(data_dir)
            except Exception as exc:
                logger.warning("Streak refresh failed via Telegram: %s", exc)
                error_notice = self._sanitize_streak_error(exc)

            if isinstance(recipients, list):
                updated_text = self._format_streaks_message(
                    recipients,
                    cache,
                    error_notice=error_notice,
                )
                reply_markup = self._build_streaks_keyboard()
                try:
                    await query.edit_message_text(
                        updated_text,
                        reply_markup=reply_markup,
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    if "message is not modified" not in str(exc).lower():
                        logger.warning("Failed to edit streak message: %s", exc)

    def build_application(self) -> Any:
        """Build and configure the python-telegram-bot Application."""
        try:
            from telegram.ext import (
                ApplicationBuilder,
                CallbackQueryHandler,
                CommandHandler,
                ContextTypes,
            )
        except ImportError as exc:
            raise TelegramExtraRequiredError(
                "python-telegram-bot is required to run the Telegram remote controller.\n"
                "Install it using: pip install -e '.[telegram]'"
            ) from exc

        if not self.bot_token:
            raise ValueError("telegram.bot_token must be configured in config.json")
        if not self.configured_chat_id:
            raise ValueError("telegram.chat_id must be configured in config.json")

        app = ApplicationBuilder().token(self.bot_token).build()

        app.add_handler(CommandHandler(["help"], self.handle_help))
        app.add_handler(CommandHandler(["status"], self.handle_status))
        app.add_handler(CommandHandler(["last"], self.handle_last))
        app.add_handler(CommandHandler(["next"], self.handle_next))
        app.add_handler(CommandHandler(["streaks"], self.handle_streaks))
        app.add_handler(CommandHandler(["start"], self.handle_start))

        app.add_handler(CommandHandler(["stop"], self.handle_stop))
        app.add_handler(CommandHandler(["pause"], self.handle_pause))
        app.add_handler(CommandHandler(["resume"], self.handle_resume))
        app.add_handler(CommandHandler(["run"], self.handle_run))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Exception while handling Telegram update: %s", context.error, exc_info=context.error)

        app.add_error_handler(error_handler)
        return app

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Run the controller long-polling loop until SIGINT/SIGTERM or stop_event."""
        stop_event = stop_event or asyncio.Event()
        remove_handlers = _install_stop_handlers(stop_event)

        app = self.build_application()
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(allowed_updates=["message", "callback_query"])
        logger.info("Telegram remote controller started polling (chat_id=%s)", self.configured_chat_id)

        try:
            await stop_event.wait()
        finally:
            logger.info("Stopping Telegram remote controller...")
            if app.updater and getattr(app.updater, "running", False):
                await app.updater.stop()
            if getattr(app, "running", False):
                await app.stop()
            await app.shutdown()
            remove_handlers()
            logger.info("Telegram remote controller stopped.")


def _install_stop_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    handlers: list[tuple[signal.Signals, Any]] = []

    def _signal_handler() -> None:
        logger.info("Received termination signal; shutting down Telegram controller.")
        stop_event.set()

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _signal_handler)
            handlers.append((sig, None))
        except (NotImplementedError, RuntimeError):
            previous = signal.signal(sig, lambda *_: _signal_handler())
            handlers.append((sig, previous))

    def _remove() -> None:
        for sig, previous in handlers:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, previous or signal.SIG_DFL)

    return _remove
