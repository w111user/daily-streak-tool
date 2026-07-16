import asyncio
import json
import logging
import platform
import threading
from pathlib import Path
from tkinter import BooleanVar, Button, Checkbutton, Entry, Frame, Label, StringVar, Text, Tk, messagebox
from tkinter import Canvas, Scrollbar
from typing import Any

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError, async_playwright

from captcha import handle_screen_time_popup
from notifier import Notifier
from sender import TikTokSender, check_cookies_valid, get_chrome_path
from video_pool import VideoPool


CONFIG_PATH = Path("config.json")
logger = logging.getLogger(__name__)


def get_default_user_data_dir() -> str:
    system = platform.system()
    if system == "Windows":
        return str(Path.home() / "AppData/Local/chrome-debug")
    if system == "Darwin":
        return str(Path.home() / "Library/Application Support/chrome-debug")
    return str(Path.home() / ".config/chrome-debug")


def get_user_data_dir_key() -> str:
    system = platform.system()
    if system == "Windows":
        return "user_data_dir_windows"
    if system == "Darwin":
        return "user_data_dir_macos"
    return "user_data_dir_linux"


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
        self.recipient_vars: list[tuple[BooleanVar, dict[str, str]]] = []

        self.config = self.load_config()
        self.schedule_time = StringVar(value=self.config.get("schedule", {}).get("time", "00:00"))
        delay = self.config.get("tiktok", {}).get("message_delay_seconds", [8, 18])
        self.delay_min = StringVar(value=str(delay[0] if len(delay) > 0 else 8))
        self.delay_max = StringVar(value=str(delay[1] if len(delay) > 1 else 18))
        self.cookie_file = StringVar(value=self.config.get("cookie_file", "cookies.json"))
        self.status = StringVar(value="Idle")

        self.build_ui()
        self.bind_autosave()

    def load_config(self) -> dict[str, Any]:
        if not CONFIG_PATH.exists():
            return {
                "schedule": {"time": "00:00", "timezone": "Asia/Ho_Chi_Minh", "run_on_start": True},
                "cookie_file": "cookies.json",
                "tiktok": {"message_delay_seconds": [8, 18]},
                "recipients": [],
                "videos": [],
            }

        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

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
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(self.config, file, indent=2)
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
            key = get_user_data_dir_key()
            user_data_dir = Path(
                self.config.get("tiktok", {}).get(key) or get_default_user_data_dir()
            ).expanduser()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=get_chrome_path(),
                headless=False,
                args=[
                    "--restore-last-session",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                ],
                ignore_default_args=["--enable-automation", "--no-first-run"],
            )
            context.set_default_timeout(int(self.config.get("tiktok", {}).get("navigation_timeout_ms", 60000)))
            try:
                await self.load_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")
                await handle_screen_time_popup(page)

                conversation_list = page.locator("[data-e2e='dm-new-conversation-list']").first
                conversation_items = page.locator("[data-e2e='dm-new-conversation-item']")
                await conversation_items.first.wait_for(state="visible")

                found: dict[str, dict[str, str]] = {}
                previous_count = -1
                stable_scrolls = 0
                for _ in range(40):
                    count = await conversation_items.count()
                    for index in range(count):
                        item = conversation_items.nth(index)
                        name = await item.locator("[data-e2e='dm-new-conversation-nickname']").inner_text()
                        name = name.strip()
                        found[name] = {"name": name, "username": ""}

                    if len(found) == previous_count:
                        stable_scrolls += 1
                    else:
                        stable_scrolls = 0
                    if stable_scrolls >= 3:
                        break

                    previous_count = len(found)
                    await conversation_list.evaluate("(element) => element.scrollBy(0, 600)")
                    await page.wait_for_timeout(500)

                return list(found.values())
            finally:
                await context.close()

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
            key = get_user_data_dir_key()
            user_data_dir = Path(
                self.config.get("tiktok", {}).get(key) or get_default_user_data_dir()
            ).expanduser()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=get_chrome_path(),
                headless=False,
                args=[
                    "--restore-last-session",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                ],
                ignore_default_args=["--enable-automation", "--no-first-run"],
            )
            context.set_default_timeout(int(self.config.get("tiktok", {}).get("navigation_timeout_ms", 60000)))
            try:
                await self.load_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")
                await handle_screen_time_popup(page)

                resolved = []
                total = len(recipients)
                for index, recipient in enumerate(recipients, start=1):
                    self.set_status_from_worker(f"Resolving {index}/{total}...")
                    display_name = recipient["name"]
                    await self.click_conversation_by_display_name(page, display_name)
                    username = await self.extract_username_from_chat(page)
                    if not username:
                        logger.warning("Skipping %s because username could not be resolved", display_name)
                        await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")
                        await handle_screen_time_popup(page)
                        continue
                    resolved.append({"name": display_name, "username": username})
                    logger.info("Resolved %s to @%s", display_name, username)
                    await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")
                    await handle_screen_time_popup(page)
                return resolved
            finally:
                await context.close()

    async def click_conversation_by_display_name(self, page: Any, display_name: str) -> None:
        conversation_list = page.locator("[data-e2e='dm-new-conversation-list']").first
        conversation_items = page.locator("[data-e2e='dm-new-conversation-item']")
        await conversation_items.first.wait_for(state="visible")

        normalized_display_name = display_name.casefold()
        for _ in range(40):
            for index in range(await conversation_items.count()):
                item = conversation_items.nth(index)
                nickname = item.locator("[data-e2e='dm-new-conversation-nickname']")
                nickname_text = (await nickname.inner_text()).strip()
                if nickname_text.casefold() == normalized_display_name:
                    await item.click()
                    await handle_screen_time_popup(page)
                    await page.wait_for_timeout(500)
                    return

            await conversation_list.evaluate("(element) => element.scrollBy(0, 600)")
            await page.wait_for_timeout(500)

        raise ValueError(f"Could not find conversation for display name: {display_name}")

    async def extract_username_from_chat(self, page: Any) -> str:
        element = page.locator("[data-e2e='chat-uniqueid']").first
        try:
            await element.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            logger.warning("Timed out waiting for chat unique id")
            return ""

        username_text = await element.inner_text()
        return username_text.strip().lstrip("@")

    async def extract_username_from_item(self, item: Any, fallback: str) -> str:
        hrefs = await item.locator("a[href^='/@'], a[href*='tiktok.com/@']").evaluate_all(
            """(links) => links.map((link) => link.getAttribute("href"))"""
        )
        for href in hrefs:
            username = self.username_from_href(href)
            if username:
                return username
        return fallback

    def username_from_href(self, href: str | None) -> str:
        if not href or "/@" not in href:
            return ""
        return href.split("/@")[-1].split("?")[0].split("/")[0].strip().lstrip("@")

    async def load_cookies(self, context: BrowserContext) -> bool:
        cookie_file = Path(self.config.get("cookie_file", "cookies.json"))
        if not cookie_file.exists():
            return False

        with cookie_file.open("r", encoding="utf-8") as file:
            cookies = json.load(file)
        if any("expirationDate" in cookie for cookie in cookies):
            same_site_map = {
                "unspecified": "None",
                "no_restriction": "None",
                "lax": "Lax",
                "strict": "Strict",
            }
            cookies = [
                {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie["domain"],
                    "path": cookie.get("path", "/"),
                    "expires": int(cookie["expirationDate"]),
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", False),
                    "sameSite": same_site_map.get(cookie.get("sameSite", "unspecified"), "None"),
                }
                for cookie in cookies
                if not cookie.get("session", False)
            ]
        await context.add_cookies(cookies)
        return True

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
