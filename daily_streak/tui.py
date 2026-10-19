"""Optional Textual frontend built on the shared Daily Streak Tool core."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from daily_streak.cli import configured_videos, status_payload
from daily_streak.config import DEFAULT_CONFIG_PATH, load_config, save_config
from daily_streak.history import RunHistoryStore
from daily_streak.models import RecipientResult, RunSummary
from daily_streak.paths import get_data_dir
from daily_streak.state import normalize_username
from daily_streak.streak_reader import StreakCache, format_streak_display


logger = logging.getLogger(__name__)

RecipientAction = Callable[..., Awaitable[list[dict[str, str]]]]
TuiSenderFactory = Callable[[Mapping[str, Any], Callable[[RecipientResult, RunSummary], None]], Any]


@dataclass
class _CatalogRecipient:
    """One TUI row and its selection state in the same coherent catalog."""

    values: dict[str, Any]
    selected: bool = False


class TuiUnavailableError(RuntimeError):
    """Raised when the optional Textual dependency is not installed."""


class TuiInputError(ValueError):
    """A recoverable TUI action error that should not produce a traceback."""


def _operation_error_status(error: Exception) -> tuple[str, bool]:
    """Return the UI status and whether an operational error should be logged."""

    if isinstance(error, TuiInputError) or bool(
        getattr(error, "expected_operation_error", False)
    ):
        return str(error), False
    return f"Operation failed: {error}", True


def _default_notifier_factory(config: Mapping[str, Any]) -> Any:
    from daily_streak.notifications import Notifier

    return Notifier(config)


class TuiController:
    """Textual-free adapter state that delegates every operation to shared core.

    Browser-backed operations are imported only when scan, resolve, or send is
    requested.  The controller itself is intentionally small and testable on a
    server that does not install Textual, Playwright, or desktop packages.
    """

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        config_loader: Callable[[str | Path], dict[str, Any]] = load_config,
        config_saver: Callable[[Mapping[str, Any], str | Path], None] = save_config,
        sender_factory: TuiSenderFactory | None = None,
        scan_action: RecipientAction | None = None,
        resolve_action: RecipientAction | None = None,
        daemon_manager: Any | None = None,
        notifier_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config_loader = config_loader
        self.config_saver = config_saver
        self.sender_factory = sender_factory
        self.scan_action = scan_action or _scan_recipients
        self.resolve_action = resolve_action or _resolve_recipients
        self.notifier_factory = notifier_factory
        if daemon_manager is not None:
            self.daemon_manager = daemon_manager
        else:
            from daily_streak.daemon import DaemonManager

            self.daemon_manager = DaemonManager(self.config_path, config_loader=self.config_loader)
        self.config: dict[str, Any] = {}
        self._catalog: list[_CatalogRecipient] = []
        self.current_results: list[RecipientResult] = []
        self.last_summary: RunSummary | None = None
        self._progress_callback: Callable[[RecipientResult, RunSummary], None] | None = None
        self.reload()

    def daemon_status(self) -> dict[str, Any]:
        """Return structured daemon status from the daemon manager."""
        return self.daemon_manager.get_status()

    def daemon_logs(self, max_lines: int = 100) -> list[str]:
        """Return recent bounded log lines from the daemon log file."""
        return self.daemon_manager.read_logs(max_lines=max_lines)

    def start_daemon(self) -> int:
        """Start the background daemon process."""
        return self.daemon_manager.start()

    def stop_daemon(self, timeout: float = 10.0) -> bool:
        """Stop the background daemon process gracefully."""
        return self.daemon_manager.stop(timeout=timeout)

    def reload(self) -> None:
        """Refresh configuration, dashboard state, history, and selected recipients."""

        self.config = self.config_loader(self.config_path)
        self.streak_cache = StreakCache(get_data_dir(self.config))
        # Configured recipients are the initial catalog, not merely a detached
        # selection set.  This makes their rows render before any browser scan.
        self._catalog = [
            _CatalogRecipient(values=dict(recipient), selected=True)
            for recipient in _raw_recipients(self.config)
        ]

    def dashboard(self) -> dict[str, object]:
        """Return the existing browser-free dashboard status representation."""

        return status_payload(self.config)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Read history through the shared durable history store."""

        return RunHistoryStore(get_data_dir(self.config)).load(limit)

    def history_rows(self, limit: int = 20) -> list[dict[str, object]]:
        """Return formatted history rows for rendering, with legacy fallback."""

        records = self.history(limit)
        rows: list[dict[str, object]] = []
        for record in records:
            timestamp = str(record.get("finished_at") or record.get("started_at") or "")
            rows.append(
                {
                    "finished": timestamp,
                    "success": int(record.get("success", 0) or 0),
                    "failed": int(record.get("failed", 0) or 0),
                    "unconfirmed": int(record.get("unconfirmed", 0) or 0),
                    "skipped": int(record.get("skipped", 0) or 0),
                }
            )
        return rows

    def videos(self) -> list[str]:
        """Return videos through the same configuration helper used by CLI."""

        return configured_videos(self.config)

    @property
    def selected_recipient_count(self) -> int:
        """Return the number of catalog recipients selected for TUI actions."""

        return len(self._selected_recipients())

    @property
    def has_selected_recipients(self) -> bool:
        """Whether a resolve or send action has a selected recipient target."""

        return bool(self._selected_recipients())

    def recipient_rows(self) -> list[dict[str, object]]:
        """Return catalog rows for rendering without exposing mutable internals."""

        rows: list[dict[str, object]] = []
        for catalog_recipient in self._catalog:
            username = str(catalog_recipient.values.get("username") or "")
            record = self.streak_cache.get(username) if username else None
            streak_display = format_streak_display(record)
            rows.append(
                {
                    "key": self._recipient_key(catalog_recipient.values),
                    "selected": catalog_recipient.selected,
                    "name": str(catalog_recipient.values.get("name") or ""),
                    "username": username,
                    "streak": streak_display,
                }
            )
        return rows


    def toggle_recipient(self, key: str) -> bool:
        """Select or deselect one catalog recipient and persist the selection."""

        for catalog_recipient in self._catalog:
            if self._recipient_key(catalog_recipient.values) == key:
                catalog_recipient.selected = not catalog_recipient.selected
                self._persist_selection()
                return catalog_recipient.selected
        raise ValueError("Recipient is not present in the current TUI catalog.")

    def select_all(self) -> None:
        """Persist every scanned/configured catalog recipient as selected."""

        for catalog_recipient in self._catalog:
            catalog_recipient.selected = True
        self._persist_selection()

    def deselect_all(self) -> None:
        """Persist an empty recipient selection while retaining the in-memory catalog."""

        for catalog_recipient in self._catalog:
            catalog_recipient.selected = False
        self._persist_selection()

    async def scan(self) -> list[dict[str, object]]:
        """Scan DM conversations in a temporary shared browser session."""

        discovered = await self.scan_action(self.config)
        self._merge_recipients(discovered)
        return self.recipient_rows()

    async def resolve(self) -> list[dict[str, object]]:
        """Resolve selected display names in a temporary shared browser session."""

        selected = self._selected_recipients()
        if not selected:
            raise TuiInputError("Select at least one recipient before resolving usernames.")
        resolved = await self.resolve_action(
            self._config_with_selected_recipients(),
            _recipient_strings(selected),
        )
        self._merge_recipients(resolved)
        self._persist_selection()
        return self.recipient_rows()

    async def refresh_streaks(self) -> list[dict[str, object]]:
        """Refresh streak statuses for selected recipients via TikTok API / Android."""

        from daily_streak.android_streak_reader import AdbError
        from daily_streak.api_streak_reader import ApiStreakError
        from daily_streak.recipient_actions import refresh_streaks

        selected = self._selected_recipients()
        targets = selected if selected else [c.values for c in self._catalog]
        if not targets:
            raise TuiInputError("No recipients available to refresh streaks.")
        try:
            await refresh_streaks(self.config, targets, force=True)
        except ApiStreakError as exc:
            raise TuiInputError(f"Không thể làm mới streak qua TikTok API: {exc}") from exc
        except AdbError as exc:
            raise TuiInputError(f"Không thể làm mới streak qua Android: {exc}") from exc
        self.streak_cache.load()
        return self.recipient_rows()

    async def send(self, *, force: bool = False) -> RunSummary:

        """Use the shared sender and expose its typed progress callbacks."""

        selected = self._selected_recipients()
        if not selected:
            raise TuiInputError("Select at least one recipient before sending.")

        self.current_results = []
        self.last_summary = None
        selected_config = self._config_with_selected_recipients()
        if self.sender_factory is None:
            from daily_streak.daemon import create_sender

            sender = create_sender(selected_config, on_recipient_result=self._on_recipient_result)
        else:
            sender = self.sender_factory(selected_config, self._on_recipient_result)
        summary = await sender.send_daily_links(force=force)
        self.last_summary = summary
        self.current_results = list(summary.results)

        if summary is not None:
            notifier = None
            if self.notifier_factory is not None:
                notifier = self.notifier_factory(selected_config)
            elif hasattr(sender, "notifier") and hasattr(sender.notifier, "batch_completion"):
                notifier = sender.notifier
            else:
                notifier = _default_notifier_factory(selected_config)

            if notifier is not None and hasattr(notifier, "batch_completion"):
                try:
                    res = notifier.batch_completion(summary)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.exception("Failed to send batch completion notification")

        return summary

    def set_progress_callback(
        self,
        callback: Callable[[RecipientResult, RunSummary], None] | None,
    ) -> None:
        """Set the adapter callback used by the concrete Textual application."""

        self._progress_callback = callback

    def _on_recipient_result(self, result: RecipientResult, summary: RunSummary) -> None:
        self.current_results = list(summary.results)
        if self._progress_callback:
            self._progress_callback(result, summary)

    def _persist_selection(self) -> None:
        self.config["recipients"] = self._selected_recipient_mappings()
        self.config_saver(self.config, self.config_path)

    def _config_with_selected_recipients(self) -> dict[str, Any]:
        """Build the exact recipient selection consumed by resolve and send."""

        selected_config = dict(self.config)
        selected_config["recipients"] = self._selected_recipient_mappings()
        return selected_config

    def _selected_recipient_mappings(self) -> list[dict[str, Any]]:
        return [
            dict(catalog_recipient.values)
            for catalog_recipient in self._catalog
            if catalog_recipient.selected
        ]

    def _selected_recipients(self) -> list[dict[str, Any]]:
        return [
            catalog_recipient.values
            for catalog_recipient in self._catalog
            if catalog_recipient.selected
        ]

    def _merge_recipients(self, discovered: Sequence[Mapping[str, str]]) -> None:
        """Merge browser discoveries without silently selecting new recipients."""

        for discovered_recipient in discovered:
            candidate = dict(discovered_recipient)
            candidate_name = str(candidate.get("name") or "").strip()
            candidate_username = str(candidate.get("username") or "").strip()
            if not candidate_name and not candidate_username:
                continue

            index = self._find_catalog_index(candidate_name, candidate_username)
            if index is None:
                self._catalog.append(_CatalogRecipient(values=candidate))
                continue

            current = self._catalog[index].values
            for field, value in candidate.items():
                if value:
                    current[field] = value

    def _find_catalog_index(self, name: str, username: str) -> int | None:
        normalized_username = normalize_username(username) if username else ""
        normalized_name = name.casefold()
        for index, catalog_recipient in enumerate(self._catalog):
            recipient = catalog_recipient.values
            existing_username = str(recipient.get("username") or "")
            existing_name = str(recipient.get("name") or "")
            if normalized_username and normalize_username(existing_username) == normalized_username:
                return index
            if normalized_name and existing_name.casefold() == normalized_name:
                return index
        return None

    @staticmethod
    def _recipient_key(recipient: Mapping[str, Any]) -> str:
        username = str(recipient.get("username") or "").strip()
        if username:
            return f"username:{normalize_username(username)}"
        name = str(recipient.get("name") or "").strip()
        return f"name:{name.casefold()}"


async def _scan_recipients(config: Mapping[str, Any]) -> list[dict[str, str]]:
    from daily_streak.recipient_actions import scan_recipients

    return await scan_recipients(config)


async def _resolve_recipients(
    config: Mapping[str, Any],
    recipients: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    from daily_streak.recipient_actions import resolve_recipients

    return await resolve_recipients(config, recipients)


def _raw_recipients(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    recipients = config.get("recipients", [])
    return [recipient for recipient in recipients if isinstance(recipient, Mapping)] if isinstance(recipients, list) else []


def _recipient_strings(recipients: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "name": str(recipient.get("name") or ""),
            "username": str(recipient.get("username") or ""),
        }
        for recipient in recipients
    ]


class _TuiLogHandler(logging.Handler):
    """Forward standard logging into RichLog without retaining unbounded output."""

    def __init__(self, app: Any) -> None:
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.exc_info and not logger.isEnabledFor(logging.DEBUG):
                formatter = self.formatter or logging.Formatter()
                message = (
                    f"{formatter.formatTime(record)} {record.levelname} "
                    f"{record.name}: {record.getMessage()}"
                )
            else:
                message = self.format(record)
            self.app.call_after_refresh(self.app.append_log, message)
        except Exception:
            # Logging must never disrupt a sender operation or terminal UI.
            pass


def run_tui(config_path: str | Path = DEFAULT_CONFIG_PATH) -> int:
    """Run the optional Textual app, importing Textual only at invocation time."""

    app = create_tui_app(config_path)
    app.run()
    return 0


def create_tui_app(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    controller: TuiController | None = None,
) -> Any:
    """Create the concrete Textual app while keeping module import dependency-free."""

    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical
        from textual.widgets import (
            Button,
            DataTable,
            Footer,
            Header,
            RichLog,
            Static,
            TabbedContent,
            TabPane,
        )
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("textual"):
            raise TuiUnavailableError(
                "Textual TUI support is not installed. Install it with: pip install 'daily-streak-tool[tui]'"
            ) from exc
        raise

    controller = controller or TuiController(config_path)

    class StreakTui(App[None]):
        """Interactive dashboard; it holds no browser resources while idle."""

        TITLE = "Daily Streak Tool v2"
        CSS = """
        #run-status { height: 3; padding: 1 2; }
        #dashboard { height: auto; padding: 1 2; }
        DataTable { height: 1fr; }
        #logs-panel { height: 14; }
        #logs { height: 1fr; border: solid $primary; padding: 0 1; }
        TabbedContent { height: 1fr; }
        #daemon-info { height: auto; padding: 1 2; }
        #daemon-buttons { height: 3; padding: 0 2; margin: 1 0; }
        #daemon-buttons Button { margin-right: 1; }
        #daemon-logs-title { height: 1; padding: 0 2; }
        #daemon-logs { height: 1fr; border: solid $primary; padding: 0 1; }
        """
        BINDINGS = [
            Binding("s", "send", "Send"),
            Binding("f", "force_send", "Force send"),
            Binding("c", "scan", "Scan"),
            Binding("r", "resolve", "Resolve"),
            Binding("t", "refresh_streaks", "Streaks"),
            Binding("space", "toggle_recipient", "Toggle"),
            Binding("a", "select_all", "Select all"),
            Binding("d", "deselect_all", "Deselect"),
            Binding("u", "start_daemon", "Start daemon"),
            Binding("k", "stop_daemon", "Stop daemon"),
            Binding("g", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.controller = controller
            self._operation_task: asyncio.Task[None] | None = None
            self._run_status = "Idle — browser launches only for send, scan, or resolve."
            self._log_handler: _TuiLogHandler | None = None
            self._recipient_rows: list[dict[str, object]] = []

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static(id="run-status")
            with TabbedContent():
                with TabPane("Dashboard", id="dashboard-tab"):
                    yield Static(id="dashboard")
                    yield DataTable(id="results")
                with TabPane("Recipients", id="recipients-tab"):
                    yield DataTable(id="recipients")
                with TabPane("Videos", id="videos-tab"):
                    yield DataTable(id="videos")
                with TabPane("History", id="history-tab"):
                    yield DataTable(id="history")
                with TabPane("Daemon", id="daemon-tab"):
                    yield Static(id="daemon-info")
                    with Horizontal(id="daemon-buttons"):
                        yield Button("Start Daemon", id="btn-start-daemon", variant="success")
                        yield Button("Stop Daemon", id="btn-stop-daemon", variant="error")
                        yield Button("Refresh Status", id="btn-refresh-daemon", variant="primary")
                        yield Button("Refresh Logs", id="btn-refresh-daemon-logs")
                    yield Static("Daemon Logs (bounded tail):", id="daemon-logs-title")
                    yield RichLog(id="daemon-logs", max_lines=200, wrap=True, highlight=False, markup=False)
            with Vertical(id="logs-panel"):
                yield Static("Live logs", id="logs-title")
                yield RichLog(id="logs", max_lines=300, wrap=True, highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#results", DataTable).add_columns(
                "Recipient", "Username", "Status", "Attempts", "Video", "Error"
            )
            self.query_one("#recipients", DataTable).add_columns("Selected", "Name", "Username", "Streak")
            self.query_one("#videos", DataTable).add_columns("#", "Video")
            self.query_one("#history", DataTable).add_columns(
                "Finished", "Success", "Failed", "Unconfirmed", "Skipped"
            )

            self._log_handler = _TuiLogHandler(self)
            self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(self._log_handler)
            self.controller.set_progress_callback(self._on_recipient_result)
            self._render_all()

        def on_unmount(self) -> None:
            if self._log_handler is not None:
                logging.getLogger().removeHandler(self._log_handler)
            self.controller.set_progress_callback(None)

        def append_log(self, message: str) -> None:
            logs = self.query_one("#logs", RichLog)
            follow_tail = logs.is_vertical_scroll_end
            logs.write(message, scroll_end=follow_tail)
            if follow_tail:
                logs.scroll_end(animate=False)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-start-daemon":
                self.action_start_daemon()
            elif event.button.id == "btn-stop-daemon":
                self.action_stop_daemon()
            elif event.button.id == "btn-refresh-daemon":
                self.action_refresh_daemon()
            elif event.button.id == "btn-refresh-daemon-logs":
                self.action_refresh_daemon_logs()

        def action_refresh(self) -> None:
            if self._busy():
                return
            try:
                self.controller.reload()
            except Exception as exc:
                self._run_status = f"Refresh failed: {exc}"
                self.append_log(self._run_status)
            else:
                self._run_status = "Refreshed configuration, status, history, and daemon."
            self._render_all()

        def action_send(self) -> None:
            if self._busy():
                return
            if not self.controller.has_selected_recipients:
                self._run_status = "Select at least one recipient before sending."
                self._render_status()
                return
            self._start_operation("Sending with normal daily resume", self.controller.send(force=False))

        def action_force_send(self) -> None:
            if self._busy():
                return
            if not self.controller.has_selected_recipients:
                self._run_status = "Select at least one recipient before force-sending."
                self._render_status()
                return
            self._start_operation("Force-sending all selected recipients", self.controller.send(force=True))

        def action_scan(self) -> None:
            self._start_operation("Scanning DM conversations", self.controller.scan())

        def action_resolve(self) -> None:
            if self._busy():
                return
            if not self.controller.has_selected_recipients:
                self._run_status = "Select at least one recipient before resolving usernames."
                self._render_status()
                return
            self._start_operation("Resolving selected usernames", self.controller.resolve())

        def action_refresh_streaks(self) -> None:
            if self._busy():
                return
            self._start_operation("Refreshing TikTok streak statuses", self.controller.refresh_streaks())

        def action_toggle_recipient(self) -> None:

            if self._busy():
                return
            table = self.query_one("#recipients", DataTable)
            row_index = table.cursor_row
            if row_index is None or not 0 <= row_index < len(self._recipient_rows):
                self._run_status = "Select a recipient row before toggling."
                self._render_status()
                return
            row = self._recipient_rows[row_index]
            selected = self.controller.toggle_recipient(str(row["key"]))
            self._run_status = f"Recipient {'selected' if selected else 'deselected'}."
            self._render_all()

        def action_select_all(self) -> None:
            if self._busy():
                return
            self.controller.select_all()
            self._run_status = "All catalog recipients selected and saved."
            self._render_all()

        def action_deselect_all(self) -> None:
            if self._busy():
                return
            self.controller.deselect_all()
            self._run_status = "All recipients deselected and saved."
            self._render_all()

        def action_quit(self) -> None:
            self.exit()

        def action_start_daemon(self) -> None:
            self._start_operation("Starting background daemon...", self._async_start_daemon())

        async def _async_start_daemon(self) -> str:
            loop = asyncio.get_running_loop()
            try:
                pid = await loop.run_in_executor(None, self.controller.start_daemon)
            except Exception as exc:
                self._render_daemon()
                raise TuiInputError(f"Could not start daemon: {exc}") from exc
            self._render_daemon()
            return f"Daemon started successfully (PID {pid})."

        def action_stop_daemon(self) -> None:
            self._start_operation("Stopping background daemon...", self._async_stop_daemon())

        async def _async_stop_daemon(self) -> str:
            loop = asyncio.get_running_loop()
            try:
                stopped = await loop.run_in_executor(None, self.controller.stop_daemon)
            except Exception as exc:
                self._render_daemon()
                raise TuiInputError(f"Could not stop daemon: {exc}") from exc
            self._render_daemon()
            if stopped:
                return "Daemon stopped successfully."
            return "Stop signal sent; daemon may be finishing an in-progress batch."

        def action_refresh_daemon(self) -> None:
            self._render_daemon()
            self._run_status = "Refreshed daemon status and logs."
            self._render_status()

        def action_refresh_daemon_logs(self) -> None:
            self._render_daemon_logs()
            self._run_status = "Refreshed daemon logs."
            self._render_status()

        def _start_operation(self, description: str, operation: Awaitable[object]) -> None:
            if self._busy():
                operation.close() if hasattr(operation, "close") else None
                return
            self._run_status = description
            self._render_status()
            self._operation_task = asyncio.create_task(self._run_operation(operation))

        async def _run_operation(self, operation: Awaitable[object]) -> None:
            try:
                result = await operation
            except asyncio.CancelledError:
                self._run_status = "Operation cancelled."
                raise
            except Exception as exc:
                self._run_status, should_log = _operation_error_status(exc)
                if should_log:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.exception("TUI operation failed")
                    else:
                        logger.error("TUI operation failed: %s", exc)
            else:
                if isinstance(result, RunSummary):
                    self._run_status = (
                        f"Finished: {result.success_count} success, {result.failed_count} failed, "
                        f"{result.unconfirmed_count} unconfirmed, {result.skipped_count} skipped."
                    )
                elif isinstance(result, str):
                    self._run_status = result
                else:
                    self._run_status = "Operation completed."
            finally:
                self._operation_task = None
                if self.is_mounted:
                    self._render_all()

        def _on_recipient_result(self, result: RecipientResult, summary: RunSummary) -> None:
            self._run_status = (
                f"{result.status.value.title()}: {result.recipient_name} "
                f"({summary.success_count} success, {summary.failed_count} failed, "
                f"{summary.unconfirmed_count} unconfirmed, {summary.skipped_count} skipped)"
            )
            self._render_status()
            self._render_results()

        def _render_all(self) -> None:
            self._render_status()
            self._render_dashboard()
            self._render_results()
            self._render_recipients()
            self._render_videos()
            self._render_history()
            self._render_daemon()

        def _render_daemon(self) -> None:
            status = self.controller.daemon_status()
            lines = [
                f"Daemon Status:       {status['status']}" + (f" (PID: {status['pid']})" if status.get("pid") else ""),
                f"Configured Schedule: {status['schedule_time']} ({status['timezone']})",
                f"Next Scheduled Run:  {status['next_run']}",
                f"Run On Start:        {'Enabled (runs immediately on start)' if status['run_on_start'] else 'Disabled'}",
            ]
            last_run = status.get("last_run")
            if last_run:
                finished_at = last_run.get("finished_at") or last_run.get("started_at") or "unknown"
                lines.append(
                    f"Last Batch Outcome:  {last_run.get('success', 0)} success, "
                    f"{last_run.get('failed', 0)} failed, {last_run.get('unconfirmed', 0)} unconfirmed, "
                    f"{last_run.get('skipped', 0)} skipped ({finished_at})"
                )
            else:
                lines.append("Last Batch Outcome:  No runs recorded yet.")

            self.query_one("#daemon-info", Static).update("\n".join(lines))
            self._render_daemon_logs()

        def _render_daemon_logs(self) -> None:
            logs = self.controller.daemon_logs(max_lines=100)
            rich_log = self.query_one("#daemon-logs", RichLog)
            rich_log.clear()
            if not logs:
                rich_log.write("No daemon logs recorded yet.")
            else:
                for line in logs:
                    rich_log.write(line)

        def _render_status(self) -> None:
            self.query_one("#run-status", Static).update(self._run_status)

        def _render_dashboard(self) -> None:
            dashboard = self.controller.dashboard()
            last_summary = self.controller.last_summary
            summary_text = "No TUI send has run yet."
            if last_summary:
                summary_text = (
                    f"Current TUI run: {last_summary.success_count} success, "
                    f"{last_summary.failed_count} failed, {last_summary.unconfirmed_count} unconfirmed, "
                    f"{last_summary.skipped_count} skipped."
                )
            self.query_one("#dashboard", Static).update(
                "\n".join(
                    (
                        f"State: {dashboard['state']}",
                        f"Recipients: {dashboard['recipients']}  Completed today: {dashboard['completed_today']}",
                        f"Videos: {dashboard['videos']}  Browser: {'headless' if dashboard['browser']['headless'] else 'headed'}",  # type: ignore[index]
                        f"Schedule: {dashboard['schedule']['time']} {dashboard['schedule']['timezone']}",  # type: ignore[index]
                        summary_text,
                    )
                )
            )

        def _render_results(self) -> None:
            table = self.query_one("#results", DataTable)
            table.clear(columns=False)
            for result in self.controller.current_results:
                table.add_row(
                    result.recipient_name,
                    result.username or "",
                    result.status.value,
                    str(result.attempts),
                    result.selected_video or "",
                    result.error_message or "",
                )

        def _render_recipients(self) -> None:
            table = self.query_one("#recipients", DataTable)
            table.clear(columns=False)
            self._recipient_rows = self.controller.recipient_rows()
            for row in self._recipient_rows:
                table.add_row(
                    "✓" if row["selected"] else "",
                    str(row["name"]),
                    f"@{row['username']}" if row["username"] else "unresolved",
                    str(row.get("streak") or "Chưa xác định"),
                )


        def _render_videos(self) -> None:
            table = self.query_one("#videos", DataTable)
            table.clear(columns=False)
            for index, video in enumerate(self.controller.videos(), start=1):
                table.add_row(str(index), video)

        def _render_history(self) -> None:
            table = self.query_one("#history", DataTable)
            table.clear(columns=False)
            for row in self.controller.history_rows():
                table.add_row(
                    str(row["finished"]),
                    str(row["success"]),
                    str(row["failed"]),
                    str(row["unconfirmed"]),
                    str(row["skipped"]),
                )

        def _busy(self) -> bool:
            if self._operation_task is None or self._operation_task.done():
                return False
            self._run_status = "An operation is already in progress."
            self._render_status()
            return True

    return StreakTui()
