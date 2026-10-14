import asyncio
import logging
import threading
from tkinter import BooleanVar, Button, Checkbutton, Entry, Frame, Label, StringVar, Text, Tk, messagebox
from tkinter import Canvas, Scrollbar
from typing import Any

from playwright.async_api import async_playwright

from daily_streak.browser import (
    check_cookies_valid,
    get_action_timeout_ms,
    get_content_ready_timeout_ms,
    get_dm_load_attempts,
    get_dm_readiness_timeout_ms,
    get_low_memory_mode,
    get_low_memory_stabilization_seconds,
    get_navigation_timeout_ms,
    get_probe_timeout_ms,
    launch_browser_session,
)
from daily_streak.config import DEFAULT_CONFIG_PATH, load_config, save_config as save_config_file
from daily_streak.recipients import RecipientService
from notifier import Notifier
from sender import TikTokSender
from video_pool import VideoPool


CONFIG_PATH = DEFAULT_CONFIG_PATH
logger = logging.getLogger(__name__)


class AsyncRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Any) -> asyncio.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class StreakSenderUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("TikTok Streak Sender")
        self.runner = AsyncRunner()
        self.sender_future: asyncio.Future | None = None
        self.scan_future: asyncio.Future | None = None
        self.resolve_future: asyncio.Future | None = None
        self.save_after_id: str | None = None
        self.config = self.load_config()
        self.recipient_vars: list[tuple[BooleanVar, dict[str, str]]] = []
        self.recipient_service = RecipientService(
            navigation_timeout_ms=get_navigation_timeout_ms(self.config),
            action_timeout_ms=get_action_timeout_ms(self.config),
            content_ready_timeout_ms=get_content_ready_timeout_ms(self.config),
            dm_readiness_timeout_ms=get_dm_readiness_timeout_ms(self.config),
            dm_load_attempts=get_dm_load_attempts(self.config),
            probe_timeout_ms=get_probe_timeout_ms(self.config),
            low_memory_mode=get_low_memory_mode(self.config),
            low_memory_stabilization_seconds=get_low_memory_stabilization_seconds(
                self.config
            ),
        )
        self.schedule_time = StringVar(value=self.config.get("schedule", {}).get("time", "00:00"))
        delay = self.config.get("tiktok", {}).get("message_delay_seconds", [8, 18])
        self.delay_min = StringVar(value=str(delay[0] if len(delay) > 0 else 8))
        self.delay_max = StringVar(value=str(delay[1] if len(delay) > 1 else 18))
        self.cookie_file = StringVar(value=self.config.get("cookie_file", "cookies.json"))
        self.status = StringVar(value="Idle")

        self.build_ui()
        self.bind_autosave()

    def load_config(self) -> dict[str, Any]:
        return load_config(CONFIG_PATH, missing_ok=True)

    def build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)

        config_frame = Frame(self.root, padx=10, pady=10)
        config_frame.grid(row=0, column=0, sticky="ew")
        config_frame.columnconfigure(1, weight=1)

        Label(config_frame, text="Config").grid(row=0, column=0, columnspan=4, sticky="w")
        Label(config_frame, text="Schedule time").grid(row=1, column=0, sticky="w")
        Entry(config_frame, textvariable=self.schedule_time).grid(row=1, column=1, sticky="ew")

        Label(config_frame, text="Delay min").grid(row=2, column=0, sticky="w")
        Entry(config_frame, textvariable=self.delay_min).grid(row=2, column=1, sticky="ew")
        Label(config_frame, text="Delay max").grid(row=2, column=2, sticky="w", padx=(10, 0))
        Entry(config_frame, textvariable=self.delay_max).grid(row=2, column=3, sticky="ew")

        Label(config_frame, text="Cookie file").grid(row=3, column=0, sticky="w")
        Entry(config_frame, textvariable=self.cookie_file).grid(row=3, column=1, columnspan=3, sticky="ew")

        Label(config_frame, text="Video links").grid(row=4, column=0, sticky="nw")
        self.video_text = Text(config_frame, height=5, width=60)
        self.video_text.grid(row=4, column=1, columnspan=3, sticky="ew")
        self.video_text.insert("1.0", "\n".join(self.config.get("videos", [])))

        scanner_frame = Frame(self.root, padx=10, pady=10)
        scanner_frame.grid(row=1, column=0, sticky="nsew")
        scanner_frame.columnconfigure(0, weight=1)
        scanner_frame.rowconfigure(2, weight=1)

        Label(scanner_frame, text="Recipient scanner").grid(row=0, column=0, sticky="w")
        Button(scanner_frame, text="Scan DM List", command=self.scan_dm_list).grid(row=1, column=0, sticky="w")
        Button(scanner_frame, text="Resolve Usernames", command=self.resolve_usernames).grid(row=1, column=1)
        Button(scanner_frame, text="Select All", command=lambda: self.set_all_recipients(True)).grid(row=1, column=2)
        Button(scanner_frame, text="Deselect All", command=lambda: self.set_all_recipients(False)).grid(row=1, column=3)

        self.recipient_canvas = Canvas(scanner_frame, height=220)
        self.recipient_canvas.grid(row=2, column=0, columnspan=4, sticky="nsew")
        scrollbar = Scrollbar(scanner_frame, orient="vertical", command=self.recipient_canvas.yview)
        scrollbar.grid(row=2, column=4, sticky="ns")
        self.recipient_canvas.configure(yscrollcommand=scrollbar.set)
        self.recipient_frame = Frame(self.recipient_canvas)
        self.recipient_canvas.create_window((0, 0), window=self.recipient_frame, anchor="nw")
        self.recipient_frame.bind(
            "<Configure>",
            lambda _: self.recipient_canvas.configure(scrollregion=self.recipient_canvas.bbox("all")),
        )

        control_frame = Frame(self.root, padx=10, pady=10)
        control_frame.grid(row=2, column=0, sticky="ew")
        Button(control_frame, text="Start Sender", command=self.start_sender).grid(row=0, column=0, sticky="w")
        Button(control_frame, text="Stop", command=self.stop_sender).grid(row=0, column=1, sticky="w", padx=(8, 0))
        Label(control_frame, textvariable=self.status).grid(row=0, column=2, sticky="w", padx=(12, 0))

        self.populate_recipients(self.config.get("recipients", []))

    def bind_autosave(self) -> None:
        for variable in (self.schedule_time, self.delay_min, self.delay_max, self.cookie_file):
            variable.trace_add("write", lambda *_: self.schedule_save())
        self.video_text.bind("<KeyRelease>", lambda _: self.schedule_save())

    def schedule_save(self) -> None:
        if self.save_after_id:
            self.root.after_cancel(self.save_after_id)
        self.save_after_id = self.root.after(500, self.save_config)

    def save_config(self, include_recipients: bool = True) -> None:
        try:
            self.config.setdefault("schedule", {})["time"] = self.schedule_time.get() or "00:00"
            self.config.setdefault("tiktok", {})["message_delay_seconds"] = [
                int(self.delay_min.get() or 8),
                int(self.delay_max.get() or 18),
            ]
            self.config["cookie_file"] = self.cookie_file.get() or "cookies.json"
            self.config["videos"] = [
                line.strip()
                for line in self.video_text.get("1.0", "end").splitlines()
                if line.strip()
            ]
            if include_recipients:
                self.config["recipients"] = [
                    recipient for checked, recipient in self.recipient_vars if checked.get()
                ]
            save_config_file(self.config, CONFIG_PATH)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def populate_recipients(self, recipients: list[dict[str, str]]) -> None:
        for child in self.recipient_frame.winfo_children():
            child.destroy()
        self.recipient_vars = []

        configured = {
            recipient.get("username") or recipient.get("name")
            for recipient in self.config.get("recipients", [])
        }
        for row, recipient in enumerate(recipients):
            key = recipient.get("username") or recipient.get("name", "")
            checked = BooleanVar(value=key in configured)
            username = recipient.get("username", "")
            label = recipient.get("name", key)
            if username:
                label = f"{label} (@{username})"
            Checkbutton(
                self.recipient_frame,
                text=label,
                variable=checked,
            ).grid(row=row, column=0, sticky="w")
            self.recipient_vars.append((checked, recipient))

    def set_all_recipients(self, value: bool) -> None:
        for checked, _ in self.recipient_vars:
            checked.set(value)

    def set_status_from_worker(self, value: str) -> None:
        self.root.after(0, lambda: self.status.set(value))

    def scan_dm_list(self) -> None:
        self.save_config(include_recipients=False)
        self.status.set("Scanning DM list...")
        self.scan_future = self.runner.submit(self.scan_dm_list_async())
        self.root.after(250, self.check_scan_result)

    def check_scan_result(self) -> None:
        if not self.scan_future:
            return
        if not self.scan_future.done():
            self.root.after(250, self.check_scan_result)
            return

        try:
            recipients = self.scan_future.result()
            self.populate_recipients(recipients)
            self.status.set(f"Found {len(recipients)} recipients")
        except Exception as exc:
            self.status.set("Scan failed")
            messagebox.showerror("Scan failed", str(exc))

    async def scan_dm_list_async(self) -> list[dict[str, str]]:
        async with async_playwright() as playwright:
            session = await launch_browser_session(playwright, self.config)
            try:
                await session.load_cookies(self.config.get("cookie_file", "cookies.json"))
                page = await session.get_page()
                return await self.recipient_service.scan_dm_list(page)
            finally:
                await session.close()

    def resolve_usernames(self) -> None:
        checked_recipients = [
            recipient for checked, recipient in self.recipient_vars if checked.get()
        ]
        if not checked_recipients:
            messagebox.showinfo("Resolve usernames", "Select at least one recipient first.")
            return

        self.save_config(include_recipients=False)
        self.status.set("Resolving usernames...")
        self.resolve_future = self.runner.submit(self.resolve_usernames_async(checked_recipients))
        self.root.after(250, self.check_resolve_result)

    def check_resolve_result(self) -> None:
        if not self.resolve_future:
            return
        if not self.resolve_future.done():
            self.root.after(250, self.check_resolve_result)
            return

        try:
            resolved = self.resolve_future.result()
            resolved_by_name = {recipient["name"]: recipient for recipient in resolved}
            for _, recipient in self.recipient_vars:
                if recipient.get("name") in resolved_by_name:
                    recipient.update(resolved_by_name[recipient["name"]])
            self.config["recipients"] = [
                recipient for checked, recipient in self.recipient_vars if checked.get()
            ]
            self.populate_recipients([recipient for _, recipient in self.recipient_vars])
            self.save_config()
            self.status.set(f"Resolved {len(resolved)} usernames")
        except Exception as exc:
            self.status.set("Resolve failed")
            messagebox.showerror("Resolve failed", str(exc))

    async def resolve_usernames_async(self, recipients: list[dict[str, str]]) -> list[dict[str, str]]:
        async with async_playwright() as playwright:
            session = await launch_browser_session(playwright, self.config)
            try:
                await session.load_cookies(self.config.get("cookie_file", "cookies.json"))
                page = await session.get_page()
                return await self.recipient_service.resolve_usernames(
                    page,
                    recipients,
                    on_progress=lambda index, total: self.set_status_from_worker(
                        f"Resolving {index}/{total}..."
                    ),
                )
            finally:
                await session.close()

    def start_sender(self) -> None:
        try:
            unresolved = [
                recipient.get("name", "recipient")
                for checked, recipient in self.recipient_vars
                if checked.get() and not recipient.get("username")
            ]
            if unresolved:
                messagebox.showerror(
                    "Resolve usernames first",
                    "Resolve usernames before starting:\n" + "\n".join(unresolved),
                )
                return

            self.save_config()
            if not check_cookies_valid(self.config.get("cookie_file", "cookies.json")):
                messagebox.showwarning(
                    "Cookies expired",
                    "Cookies expired or invalid. Please re-export from EditThisCookie.",
                )
                self.status.set("Cookies expired or invalid")
                return

            notifier = Notifier(self.config)
            video_pool = VideoPool(self.config)
            sender = TikTokSender(self.config, notifier, video_pool)
            self.sender_future = self.runner.submit(sender.send_daily_links())
            self.status.set("Sender running")
            self.root.withdraw()
            self.root.after(500, self.check_sender_result)
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))

    def check_sender_result(self) -> None:
        if not self.sender_future:
            return
        if self.sender_future.cancelled():
            self.status.set("Sender stopped")
            return
        if not self.sender_future.done():
            self.root.after(500, self.check_sender_result)
            return

        self.root.deiconify()
        try:
            self.sender_future.result()
            self.status.set("Sender finished")
        except Exception as exc:
            self.status.set("Sender failed")
            messagebox.showerror("Sender failed", str(exc))

    def stop_sender(self) -> None:
        if self.sender_future and not self.sender_future.done():
            self.sender_future.cancel()
            self.status.set("Stopping sender...")
        else:
            self.status.set("Idle")
        self.root.deiconify()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = Tk()
    StreakSenderUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
