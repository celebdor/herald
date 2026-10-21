"""Textual TUI: ReviewScreen for interactive review and StatusUpdateApp for the full pipeline."""

import asyncio
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
from rich.text import Text

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, ProgressBar, RichLog, Static

from .agent import _resolve_coding_agent, _run_agent_interactive
from .config import UserPrefs
from .gather import (
    _drafts_path,
    _prune_issue,
    batch_draft,
    load_current_statuses,
    run_gather,
    save_drafts,
    save_draft_input,
)
from .jira import (
    format_jira_field, get_issue_fields, get_jira_auth, jira_field_value,
    update_jira_status,
)
from .llm import (
    TokenUsage,
    build_system_prompt,
    create_client,
    draft_ryg,
    fetch_model_pricing,
    request_changes_api,
)
from .validation import validate_draft

def extract_color(draft: str) -> str:
    """Extract an unambiguous R/Y/G color, otherwise report Unknown."""
    color, _ = validate_draft(draft)
    return color or "Unknown"


async def publish_validated_draft(update, session, jira_url, auth_headers, key, draft):
    """Call the Jira writer only when the complete draft passes validation."""
    _, error = validate_draft(draft)
    if error:
        return None, error
    return await update(session, jira_url, auth_headers, key, draft), None


@dataclass
class ReviewResult:
    key: str
    action: str   # "approved", "skipped", "edited", "revised"
    color: str    # "Green", "Yellow", "Red", or "Unknown"
    text: str
    publication_state: str = "pending"  # "pending", "published", "failed", "unconfirmed"
    publication_error: Optional[str] = None


def edit_in_editor(draft_text: str) -> Optional[str]:
    """Open $EDITOR with draft text. Returns edited text or None if cancelled."""
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(draft_text)
        f.write(
            "\n\n# ---- Edit above this line. Lines starting with # are ignored. ----\n"
            "# Save and quit to apply. Delete all content and quit to cancel.\n"
        )
        tmppath = f.name

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    try:
        subprocess.run([editor, tmppath], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Editor failed: {e}", file=__import__("sys").stderr)
        os.unlink(tmppath)
        return None

    with open(tmppath) as f:
        lines = [line for line in f.readlines() if not line.startswith("#")]
    os.unlink(tmppath)
    result = "".join(lines).strip()
    return result if result else None


class ScrollPane(RichLog):
    """A focusable RichLog split: j/k scroll a line, ctrl-f/b page, g/G jump.

    Move focus between splits with Ctrl-w j/k (see ReviewScreen.on_key).
    """

    BINDINGS = [
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
        Binding("ctrl+f", "page_down", "Page down", show=False),
        Binding("ctrl+b", "page_up", "Page up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
    ]


class ReviewScreen(Screen):
    """Interactive review: DataTable + new draft pane + previous status pane.

    The three panes behave like vim splits: Ctrl-w j/k moves focus between
    them, and within the focused split j/k move the row cursor (table) or
    scroll the content (draft / previous-status).
    """

    # Splits top-to-bottom, for Ctrl-w j/k navigation.
    SPLITS = ("#issue-table", "#draft-panel", "#current-panel")

    CSS = """
    DataTable {
        height: 1fr;
        min-height: 5;
    }
    DataTable:focus {
        border: tall $success;
    }
    #draft-panel {
        height: 1fr;
        border: tall $accent;
        padding: 1;
    }
    #draft-panel:focus {
        border: tall $success;
    }
    #current-panel {
        height: 1fr;
        border: tall $surface;
        padding: 1;
        color: $text-muted;
    }
    #current-panel:focus {
        border: tall $success;
    }
    #request-input {
        display: none;
        height: 3;
        border: tall $warning;
    }
    #search-input {
        display: none;
        height: 3;
        border: tall $accent;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("a", "approve", "Publish to Jira", show=True),
        Binding("p", "retry_publish", "Retry publish", show=True),
        Binding("s", "skip", "Skip", show=True),
        Binding("e", "edit", "Edit in $EDITOR", show=True),
        Binding("i", "interactive_changes", "Interactive changes", show=True),
        Binding("r", "refresh", "Refresh issue & re-draft", show=True),
        Binding("R", "refresh_all", "Refresh all & re-draft", show=True),
        Binding("q", "quit_review", "Quit", show=True),
        # Vim-style navigation between issues (active while the table is focused).
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        # Vim-style search: / to search, n/N to cycle matches.
        Binding("slash", "search", "Search", show=False),
        Binding("n", "search_next", "Next match", show=False),
        Binding("N", "search_prev", "Prev match", show=False),
    ]

    def __init__(
        self, issues, drafts, summaries, current_statuses,
        client, model, usage, jira_url, auth_headers,
        date_dir: Optional[Path] = None,
        prefs: Optional[UserPrefs] = None,
        scripts_dir: Optional[Path] = None,
        args=None,
        system_prompt: str = "",
        outcomes: Optional[dict[str, str]] = None,
    ):
        super().__init__()
        self.issues = issues
        self.drafts: dict[str, str] = dict(drafts)
        self.summaries = summaries
        self.current_statuses = current_statuses
        self.client = client
        self.model = model
        self.usage = usage
        self.jira_url = jira_url
        self.auth_headers = auth_headers
        self.date_dir = date_dir
        self.prefs = prefs or UserPrefs()
        self.scripts_dir = scripts_dir
        self.args = args
        self.system_prompt = system_prompt
        self.outcomes = outcomes or {}
        self.results: dict[str, ReviewResult] = {}
        self._publication_tasks: dict[str, object] = {}
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._col_keys = {}
        self._field_generation: dict[str, int] = {}
        # Vim-style search state.
        self._search_matches: list[int] = []
        self._search_idx = 0
        # Pending Ctrl-w window command (vim split navigation).
        self._pending_window = False

    def _save_drafts(self) -> None:
        if self.date_dir:
            save_drafts(self.date_dir, self.drafts)

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="issue-table")
        yield ScrollPane(id="draft-panel", highlight=True, markup=True, auto_scroll=False)
        yield ScrollPane(id="current-panel", highlight=True, markup=True, auto_scroll=False)
        yield Input(placeholder="Describe the changes you want...", id="request-input")
        yield Input(placeholder="Search issues by key or summary...", id="search-input")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        table.cursor_type = "row"
        self._col_keys = {
            column.key: table.add_column(Text(column.label), key=column.key)
            for column in self.prefs.resolved_columns
        }
        for issue in self.issues:
            key = issue["key"]
            draft = self.drafts.get(key, "")
            color = extract_color(draft)
            values = {
                "key": key,
                "summary": (issue.get("summary") or "")[:60],
                "color": color,
                "status": issue.get("status") or "-",
                "assignee": issue.get("assignee") or "-",
                "target_version": "…",
                "action": {
                    "quiet_skip": "Quiet skip",
                    "missing_data": "No data",
                    "draft_failed": "Draft failed",
                    "invalid_draft": "Invalid draft",
                }.get(self.outcomes.get(key), "-"),
            }
            table.add_row(
                *(values.get(column.key, "…") for column in self.prefs.resolved_columns),
                key=key,
            )
        if self.issues:
            table.move_cursor(row=0)
            self._update_panels()
        self._update_status_bar()
        self.run_worker(self._load_jira_columns([issue["key"] for issue in self.issues]))

    async def _load_jira_columns(self, keys: list[str]) -> None:
        """Load display metadata without delaying review or LLM drafting."""
        columns = [
            column for column in self.prefs.resolved_columns
            if column.field or column.key == "target_version"
        ]
        if not columns or not keys:
            return
        field_ids = []
        for column in columns:
            if column.field:
                field_ids.append(column.field.split(".")[0])
            else:
                field_ids.extend((self.prefs.target_version_field, "fixVersions"))
        field_ids = list(dict.fromkeys(field_ids))
        generations = {}
        for key in keys:
            generations[key] = self._field_generation.get(key, 0) + 1
            self._field_generation[key] = generations[key]
            for column in columns:
                self._update_cell(key, column.key, "…")
        sem = asyncio.Semaphore(10)
        failed = []
        async with aiohttp.ClientSession() as session:
            async def load(key):
                async with sem:
                    try:
                        fields = await get_issue_fields(
                            session, self.jira_url, self.auth_headers, key,
                            field_ids,
                        )
                    except Exception:
                        fields = None
                        failed.append(key)
                    # A refresh may have started while this request was in flight.
                    if self._field_generation[key] == generations[key]:
                        for column in columns:
                            if fields is None:
                                value = "Unavailable"
                            else:
                                raw = (
                                    jira_field_value(fields, column.field) if column.field else
                                    fields.get(self.prefs.target_version_field) or fields.get("fixVersions")
                                )
                                value = format_jira_field(raw)
                            self._update_cell(key, column.key, Text(value))
            await asyncio.gather(*(load(key) for key in keys))
        if failed:
            self.app.notify(
                f"Could not load Jira columns for {len(failed)} issue(s). "
                "Check Jira access and configured field references; r/R retries.",
                severity="warning",
            )

    def on_data_table_row_highlighted(self) -> None:
        self._update_panels()

    def on_key(self, event: events.Key) -> None:
        """Handle the Ctrl-w window prefix for vim-style split navigation."""
        # Never intercept while typing in a text input.
        if isinstance(self.focused, Input):
            return
        if self._pending_window:
            self._pending_window = False
            if event.key in ("j", "k", "w"):
                event.stop()
                event.prevent_default()
                self._move_split(event.key)
            return
        if event.key == "ctrl+w":
            self._pending_window = True
            event.stop()
            event.prevent_default()

    def _move_split(self, key: str) -> None:
        """Move focus between the stacked splits. j=down, k=up (no wrap), w=cycle."""
        panes = [self.query_one(sel) for sel in self.SPLITS]
        current = self.focused
        idx = panes.index(current) if current in panes else 0
        if key == "j":
            idx = min(idx + 1, len(panes) - 1)
        elif key == "k":
            idx = max(idx - 1, 0)
        else:  # "w": cycle forward with wrap
            idx = (idx + 1) % len(panes)
        panes[idx].focus()

    def _current_key(self) -> Optional[str]:
        table = self.query_one("#issue-table", DataTable)
        if table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return row_key.value
        except Exception:
            return None

    def _update_panels(self) -> None:
        key = self._current_key()
        if not key:
            return

        draft = self.drafts.get(key, "(no draft)")
        draft_panel = self.query_one("#draft-panel", RichLog)
        draft_panel.clear()
        draft_panel.write(f"[bold]New draft — {key}[/bold]\n\n{draft}")
        result = self.results.get(key)
        if result and result.publication_error:
            draft_panel.write(
                f"\n\n[bold red]Publication {result.publication_state}[/bold red]\n"
                f"{result.publication_error}\nPress p to retry."
            )
        draft_panel.scroll_home(animate=False)

        previous = self.current_statuses.get(key, "")
        current_panel = self.query_one("#current-panel", RichLog)
        current_panel.clear()
        if previous:
            current_panel.write(f"[bold]Previous status — {key}[/bold]\n\n{previous}")
        else:
            current_panel.write(f"[bold]Previous status — {key}[/bold]\n\n[dim](none filed)[/dim]")
        current_panel.scroll_home(animate=False)

    def _update_draft_panel(self) -> None:
        self._update_panels()

    def _update_status_bar(self) -> None:
        approved = sum(
            1 for r in self.results.values()
            if r.action in ("approved", "edited", "revised")
        )
        skipped = sum(1 for r in self.results.values() if r.action == "skipped")
        total = len(self.issues)
        self.query_one("#status-bar", Static).update(
            f"  {approved + skipped}/{total} reviewed  |  "
            f"{approved} approved  {skipped} skipped  |  "
            f"j/k=nav  /=search  n/N=match  ^w j/k=panes  "
            f"a=publish  p=retry  s=skip  e=edit  i=revise  r=refresh  R=refresh all  q=quit"
        )

    def _mark_action(self, key: str, label: str) -> None:
        self._update_cell(key, "action", label)
        self._update_status_bar()

    def _update_cell(self, key: str, column: str, value: str | Text) -> None:
        """Update a visible column; hidden columns do not affect review actions."""
        if column in self._col_keys:
            self.query_one("#issue-table", DataTable).update_cell(
                key, self._col_keys[column], value, update_width=True,
            )

    def _update_issue_metadata(self, issues: list[dict]) -> None:
        refreshed = {issue["key"]: issue for issue in issues}
        for issue in self.issues:
            key = issue["key"]
            if key in refreshed:
                issue.update(refreshed[key])
                for column in ("summary", "status", "assignee"):
                    value = issue.get(column) or "-"
                    self._update_cell(key, column, value[:60] if column == "summary" else value)

    def _advance(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)
        self._update_draft_panel()

    def action_cursor_down(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_cursor_up(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        if table.cursor_row is not None and table.cursor_row > 0:
            table.move_cursor(row=table.cursor_row - 1)

    def action_cursor_top(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        if table.row_count:
            table.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self.query_one("#issue-table", DataTable)
        if table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def action_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.display = True
        inp.focus()

    def _do_search(self, query: str) -> None:
        inp = self.query_one("#search-input", Input)
        inp.display = False
        inp.clear()
        table = self.query_one("#issue-table", DataTable)
        table.focus()
        query = query.strip().lower()
        if not query:
            return
        self._search_matches = [
            i for i, issue in enumerate(self.issues)
            if query in issue["key"].lower()
            or query in (issue.get("summary") or "").lower()
        ]
        if not self._search_matches:
            self.app.notify(f"No match for '{query}'", severity="warning")
            return
        self._search_idx = 0
        table.move_cursor(row=self._search_matches[0])

    def _jump_match(self, delta: int) -> None:
        if not self._search_matches:
            self.app.notify("No active search", severity="warning")
            return
        self._search_idx = (self._search_idx + delta) % len(self._search_matches)
        self.query_one("#issue-table", DataTable).move_cursor(
            row=self._search_matches[self._search_idx]
        )

    def action_search_next(self) -> None:
        self._jump_match(1)

    def action_search_prev(self) -> None:
        self._jump_match(-1)

    def action_approve(self) -> None:
        key = self._current_key()
        if not key:
            return
        draft = self.drafts.get(key, "")
        if key in self._publication_tasks:
            return
        result = self.results.get(key)
        if result and result.publication_state in ("pending", "published"):
            return
        color, error = validate_draft(draft)
        if error:
            self._update_cell(key, "color", "Unknown")
            self._update_cell(key, "action", "Invalid draft")
            self.app.notify(error, title="Cannot publish", severity="error")
            return
        if result is None or result.action == "skipped":
            result = ReviewResult(key=key, action="approved", color=color, text=draft)
            self.results[key] = result
        else:
            result.text = draft
            result.color = color
        result.publication_state = "pending"
        result.publication_error = None
        self._mark_action(key, "Publishing…")
        self._start_jira_update(key, draft)
        self._advance()

    def action_retry_publish(self) -> None:
        key = self._current_key()
        if not key:
            return
        result = self.results.get(key)
        if result is None or result.publication_state not in ("failed", "unconfirmed"):
            self.app.notify(f"{key} has no failed publication to retry", severity="warning")
            return
        self.action_approve()

    def _start_jira_update(self, key: str, draft: str) -> None:
        self._publication_tasks[key] = self.run_worker(
            self._do_jira_update(key, draft), name=f"publish-{key}", exclusive=False
        )

    def action_skip(self) -> None:
        key = self._current_key()
        if not key:
            return
        if key in self._publication_tasks:
            self.app.notify(f"{key} is currently publishing", severity="warning")
            return
        draft = self.drafts.get(key, "")
        color = extract_color(draft)
        self.results[key] = ReviewResult(key=key, action="skipped", color=color, text=draft)
        self._mark_action(key, "Skipped")
        self._advance()

    def action_edit(self) -> None:
        key = self._current_key()
        if not key:
            return
        draft = self.drafts.get(key, "")
        with self.app.suspend():
            edited = edit_in_editor(draft)
        if edited:
            self.drafts[key] = edited
            _, validation_error = validate_draft(edited)
            self.outcomes[key] = "invalid_draft" if validation_error else "drafted"
            self._save_drafts()
            self._update_cell(key, "color", extract_color(edited))
            self._update_cell(key, "action", "Invalid draft" if validation_error else "-")
            self._update_draft_panel()

    def action_interactive_changes(self) -> None:
        inp = self.query_one("#request-input", Input)
        inp.display = True
        inp.focus()

    def action_refresh(self) -> None:
        key = self._current_key()
        if not key or not self.scripts_dir or not self.args:
            self.app.notify("Refresh not available", severity="error")
            return
        if key in self._publication_tasks:
            self.app.notify(f"{key} is currently publishing", severity="warning")
            return
        self.run_worker(self._do_refresh(key), exclusive=False)

    async def _do_refresh(self, key: str) -> None:
        self.run_worker(self._load_jira_columns([key]))
        draft_panel = self.query_one("#draft-panel", RichLog)
        draft_panel.loading = True
        self._update_cell(key, "action", "Refreshing…")
        try:
            date_dir, manifest = await run_gather(
                self.scripts_dir, self.args,
                self.args.output_dir if hasattr(self.args, "output_dir") else ".work/weekly-status",
                refresh=True, issue_keys=[key],
            )

            self.date_dir = date_dir
            self._update_issue_metadata(manifest["issues"])
            issue_file = self.date_dir / "issues" / f"{key}.json"
            if not issue_file.exists():
                self.outcomes[key] = "missing_data"
                self._update_cell(key, "action", "No data")
                self.app.notify(f"No data file for {key} after refresh", severity="error")
                return

            issue_data = json.loads(issue_file.read_text())
            pruned = _prune_issue(issue_data, manifest)
            summary = json.dumps(pruned, indent=2)
            self.summaries[key] = summary

            self._update_cell(key, "action", "Drafting…")
            draft = await draft_ryg(
                self.client, self.model, self.system_prompt, summary, self.usage,
            )
            self.drafts[key] = draft
            _, validation_error = validate_draft(draft)
            self.outcomes[key] = "invalid_draft" if validation_error else "drafted"
            self._save_drafts()
            save_draft_input(self.date_dir, key, summary)
            self._update_cell(key, "color", extract_color(draft))
            self._update_cell(key, "action", "Invalid draft" if validation_error else "-")
            self.results.pop(key, None)
            self._update_draft_panel()
            self.app.notify(f"{key} refreshed")
        except Exception as e:
            self.outcomes[key] = "draft_failed"
            self._update_cell(key, "action", "Draft failed")
            self.app.notify(str(e), title="Refresh failed", severity="error")
        finally:
            draft_panel.loading = False

    def action_refresh_all(self) -> None:
        if not self.scripts_dir or not self.args:
            self.app.notify("Refresh not available", severity="error")
            return
        if self._publication_tasks:
            self.app.notify("Wait for Jira publications before refreshing", severity="warning")
            return
        self.run_worker(self._do_refresh_all(), exclusive=False)

    async def _do_refresh_all(self) -> None:
        self.run_worker(self._load_jira_columns([issue["key"] for issue in self.issues]))
        draft_panel = self.query_one("#draft-panel", RichLog)
        draft_panel.loading = True
        for issue in self.issues:
            self._update_cell(issue["key"], "action", "Refreshing…")
        try:
            output_dir = getattr(self.args, "output_dir", ".work/weekly-status")
            date_dir, manifest = await run_gather(
                self.scripts_dir, self.args, output_dir,
                refresh=True,
            )
            self.date_dir = date_dir
            self._update_issue_metadata(manifest["issues"])
            for issue in self.issues:
                self._update_cell(issue["key"], "action", "Drafting…")

            if self.date_dir:
                drafts_path = _drafts_path(self.date_dir)
                if drafts_path.exists():
                    drafts_path.unlink()

            drafts, summaries, failed, outcomes = await batch_draft(
                self.client, self.model, self.system_prompt, date_dir,
                manifest["issues"], self.usage, manifest=manifest,
                force_redraft=True,
            )
            for key in outcomes:
                self.drafts.pop(key, None)
            self.drafts.update(drafts)
            self.outcomes.update(outcomes)
            if failed:
                categories = ", ".join(
                    f"{name}: {sum(outcomes.get(key) == state for key in failed)}"
                    for name, state in (
                        ("no data", "missing_data"),
                        ("draft failures", "draft_failed"),
                        ("invalid output", "invalid_draft"),
                    )
                )
                self.app.notify(
                    f"{categories}. Use r to retry or e/i to recover drafts.",
                    title="Drafts need attention",
                    severity="warning",
                )
            self.summaries.update(summaries)
            self._save_drafts()
            for issue in self.issues:
                key = issue["key"]
                draft = self.drafts.get(key, "")
                self._update_cell(key, "color", extract_color(draft))
                self._update_cell(key, "action", {
                    "quiet_skip": "Quiet skip",
                    "missing_data": "No data",
                    "draft_failed": "Draft failed",
                    "invalid_draft": "Invalid draft",
                }.get(self.outcomes.get(key), "-"))
                self.results.pop(key, None)
            self._update_panels()
            self.app.notify(f"All {len(manifest['issues'])} issues refreshed")
        except Exception as e:
            self.app.notify(str(e), title="Refresh all failed", severity="error")
        finally:
            draft_panel.loading = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._do_search(event.value)
            return
        instruction = event.value.strip()
        inp = self.query_one("#request-input", Input)
        inp.display = False
        inp.clear()
        if not instruction:
            return
        key = self._current_key()
        if not key:
            return

        draft = self.drafts.get(key, "")
        summary = self.summaries.get(key, "")

        agent = _resolve_coding_agent(self.prefs or UserPrefs())
        if agent:
            try:
                with self.app.suspend():
                    revised = _run_agent_interactive(
                        agent, self.model, draft, summary, instruction,
                        issue_key=key, date_dir=self.date_dir,
                    )
                self.drafts[key] = revised
                _, validation_error = validate_draft(revised)
                self.outcomes[key] = "invalid_draft" if validation_error else "drafted"
                self._save_drafts()
                self._update_cell(key, "color", extract_color(revised))
                self._update_cell(key, "action", "Invalid draft" if validation_error else "-")
                self._update_draft_panel()
            except Exception as e:
                self.app.notify(str(e), title="Revision failed", severity="error")
        else:
            self.run_worker(
                self._do_request_changes(key, draft, summary, instruction),
                exclusive=False,
            )

    async def _do_request_changes(self, key, draft, summary, instruction):
        try:
            revised = await request_changes_api(
                self.client, self.model, draft, summary, instruction, self.usage,
            )
            self.drafts[key] = revised
            _, validation_error = validate_draft(revised)
            self.outcomes[key] = "invalid_draft" if validation_error else "drafted"
            self._save_drafts()
            self._update_cell(key, "color", extract_color(revised))
            self._update_cell(key, "action", "Invalid draft" if validation_error else "-")
            self._update_draft_panel()
        except Exception as e:
            self.app.notify(str(e), title="Revision failed", severity="error")

    async def _do_jira_update(self, key: str, draft: str) -> None:
        result = self.results[key]
        try:
            if self._aiohttp_session is None or self._aiohttp_session.closed:
                self._aiohttp_session = aiohttp.ClientSession()
            update, error = await publish_validated_draft(
                update_jira_status, self._aiohttp_session,
                self.jira_url, self.auth_headers, key, draft,
            )
            if error:
                result.publication_state = "failed"
                result.publication_error = error
                self._mark_action(key, "Invalid draft")
                self.app.notify(error, title="Cannot publish", severity="error")
                return
            result.publication_state = (
                "published" if update.ok else
                "unconfirmed" if update.state == "unconfirmed" else "failed"
            )
            result.publication_error = update.error
            if update.ok:
                self._mark_action(key, "✓ Published")
            else:
                self._mark_action(key, "✗ FAILED" if update.state == "rejected" else "? UNCONFIRMED")
                self.app.notify(
                    f"{key}: {update.error} (press p to retry)",
                    title="Jira publication failed", severity="error",
                )
        except Exception as exc:
            result.publication_state = "failed"
            result.publication_error = f"Jira request failed: {exc}"
            self._mark_action(key, "✗ FAILED")
            self.app.notify(
                f"{key}: {result.publication_error} (press p to retry)",
                title="Jira publication failed", severity="error",
            )
        finally:
            self._publication_tasks.pop(key, None)

    async def action_quit_review(self) -> None:
        pending = list(self._publication_tasks.values())
        if pending:
            self.app.notify(
                f"Waiting for {len(pending)} Jira publication(s); results will be included before closing.",
                title="Publication pending", severity="warning",
            )
            # Workers own these awaits; wait until each task reaches a final state.
            while self._publication_tasks:
                await asyncio.sleep(0.05)
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
        self.dismiss(list(self.results.values()))


class StatusUpdateApp(App):
    """Full-pipeline TUI: gather → draft → interactive review → summary."""

    CSS = """
    #phase-label {
        height: 1;
        padding: 0 1;
        background: $accent;
        color: $text;
    }
    #progress {
        height: 1;
        margin: 0 1;
    }
    #log {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, args, prefs: UserPrefs, scripts_dir: Path):
        super().__init__()
        self.args = args
        self.prefs = prefs
        self.scripts_dir = scripts_dir

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Initializing...", id="phase-label")
        yield ProgressBar(id="progress", total=100)
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run_pipeline(), exclusive=True)

    def _set_phase(self, phase: str, total: int = 100) -> None:
        self.query_one("#phase-label", Static).update(f"[bold]{phase}[/bold]")
        pb = self.query_one("#progress", ProgressBar)
        pb.total = total
        pb.progress = 0

    def _log(self, message: str) -> None:
        self.query_one(RichLog).write(message)

    def _make_callback(self):
        async def callback(current: int, total: int, message: str) -> None:
            pb = self.query_one("#progress", ProgressBar)
            pb.total = total
            pb.progress = current
            self.query_one(RichLog).write(message)
        return callback

    async def _run_pipeline(self) -> None:
        args = self.args
        prefs = self.prefs

        try:
            self._set_phase("Gathering data...")
            self._log("Starting data gather...")

            async def _gather_log(line: str) -> None:
                self._log(line)

            refresh = getattr(args, "refresh", False)
            date_dir, manifest = await run_gather(
                self.scripts_dir, args, args.output_dir,
                line_callback=_gather_log,
                refresh=refresh,
            )
            issue_count = len(manifest["issues"])
            self._log(f"[green]✓[/green] Gathered {issue_count} issues")

            self._set_phase("Drafting status updates...", total=issue_count)
            self._log("Starting batch LLM drafting...")
            client = create_client()
            model = args.model or prefs.model
            usage = TokenUsage()
            system_prompt = build_system_prompt(prefs)
            only_significant = getattr(args, "skip_quiet", False)

            drafts, summaries, failed, outcomes = await batch_draft(
                client, model, system_prompt, date_dir,
                manifest["issues"], usage,
                callback=self._make_callback(),
                only_significant=only_significant,
                manifest=manifest,
                force_redraft=getattr(args, "redraft", False),
            )
            quiet = sum(state == "quiet_skip" for state in outcomes.values())
            missing = sum(state == "missing_data" for state in outcomes.values())
            invalid = sum(state == "invalid_draft" for state in outcomes.values())
            self._log(f"  Quiet skips: {quiet}; missing data: {missing}; invalid drafts: {invalid}")
            self._log(
                f"[green]✓[/green] Drafted {len(drafts)} issues ({usage.calls} API calls)"
            )
            if failed:
                drafting = sum(state == "draft_failed" for state in outcomes.values())
                self._log(
                    f"[yellow]⚠[/yellow] Needs attention — missing data: {missing}; "
                    f"drafting failures: {drafting}; invalid drafts: {invalid}"
                )

            current_statuses = load_current_statuses(date_dir, [i["key"] for i in manifest["issues"]])

            self._set_phase("Review")
            try:
                auth_headers = get_jira_auth()
            except ValueError as e:
                self._log(f"[red]Error: {e}[/red]")
                return

            results = await self.push_screen_wait(
                ReviewScreen(
                    manifest["issues"], drafts, summaries, current_statuses,
                    client, model, usage, prefs.jira_url, auth_headers,
                    date_dir=date_dir,
                    prefs=prefs,
                    scripts_dir=self.scripts_dir,
                    args=self.args,
                    system_prompt=system_prompt,
                    outcomes=outcomes,
                )
            ) or []

            self._set_phase("Done")
            approved = [r for r in results if r.action in ("approved", "edited", "revised")]
            published = [r for r in approved if r.publication_state == "published"]
            failed_publications = [r for r in approved if r.publication_state in ("failed", "unconfirmed")]
            pending_publications = [r for r in approved if r.publication_state == "pending"]
            skipped = [r for r in results if r.action == "skipped"]
            green = sum(1 for r in published if r.color == "Green")
            yellow = sum(1 for r in published if r.color == "Yellow")
            red = sum(1 for r in published if r.color == "Red")
            pricing = fetch_model_pricing(model)
            cost_str = f"${usage.cost(pricing):.4f}" if pricing else "unknown"

            self._log(f"\n[bold]Summary[/bold]")
            self._log(
                f"  Updated: {len(published)} "
                f"(Green: {green}, Yellow: {yellow}, Red: {red})"
            )
            self._log(f"  Failed: {len(failed_publications)}")
            self._log(f"  Pending: {len(pending_publications)}")
            self._log(f"  Skipped: {len(skipped)}")
            self._log(
                f"  LLM: {usage.calls} calls, "
                f"{usage.input_tokens + usage.output_tokens} tokens, "
                f"{cost_str} ({model})"
            )
            if published:
                self._log("\n[bold]Updated issues:[/bold]")
                tags = {"Green": "green", "Yellow": "yellow", "Red": "red"}
                for r in published:
                    tag = tags.get(r.color, "white")
                    self._log(f"  [{tag}]{r.color}[/{tag}] {r.key}")
            if failed_publications:
                self._log("\n[bold red]Publication failures:[/bold red]")
                for r in failed_publications:
                    self._log(f"  {r.key}: {r.publication_error or r.publication_state}")

            self._log("\n[dim]Press q to exit.[/dim]")

        except Exception as e:
            import traceback
            self._log(f"[red]Pipeline failed: {e}[/red]")
            self._log(traceback.format_exc())
            self._log("\n[dim]Press q to exit.[/dim]")
