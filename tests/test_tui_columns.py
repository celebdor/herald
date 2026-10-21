"""Preferences and headless review tests; no network or credentials required."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App
from textual.widgets import DataTable

from herald.app import ReviewScreen
from herald.config import DEFAULT_COLUMNS, UserPrefs, load_prefs
from herald.jira import format_jira_field, get_issue_fields, jira_field_value
from herald.llm import TokenUsage


@pytest.mark.parametrize("content", ["", '[llm]\nmodel = "test"'])
def test_default_columns(tmp_path, content):
    path = tmp_path / "prefs.toml"
    assert load_prefs(path).columns == list(DEFAULT_COLUMNS)
    path.write_text(content)
    assert load_prefs(path).columns == list(DEFAULT_COLUMNS)


def test_columns_from_prefs(tmp_path):
    path = tmp_path / "prefs.toml"
    path.write_text('[tui]\ncolumns = ["action", "target_version", "key"]\n'
                    '[jira]\ntarget_version_field = "fixVersions"')
    prefs = load_prefs(path)
    assert prefs.columns == ["action", "target_version", "key"]
    assert prefs.target_version_field == "fixVersions"


@pytest.mark.parametrize("value", ['[]', '"key"', '["key", "key"]', '["typo"]', '[1]'])
def test_invalid_columns(tmp_path, value):
    path = tmp_path / "prefs.toml"
    path.write_text(f"[tui]\ncolumns = {value}")
    with pytest.raises(ValueError, match=r"\[tui\] columns"):
        load_prefs(path)


@pytest.mark.parametrize("value", ['""', 'false', '[]'])
def test_invalid_version_field(tmp_path, value):
    path = tmp_path / "prefs.toml"
    path.write_text(f"[jira]\ntarget_version_field = {value}")
    with pytest.raises(ValueError, match="target_version_field"):
        load_prefs(path)


@pytest.mark.parametrize(("fields", "expected"), [
    ({"customfield_10855": [{"name": "4.22"}, {"name": "4.23"}, {"name": "4.22"}],
      "fixVersions": [{"name": "4.21"}]}, ["4.22", "4.23"]),
    ({"customfield_10855": [], "fixVersions": [{"name": "4.21"}]}, ["4.21"]),
    ({"fixVersions": []}, []),
    ({"customfield_10855": "4.22"}, ["4.22"]),
    ({"customfield_10855": {"value": "4.22"}}, ["4.22"]),
])
def test_target_versions(fields, expected):
    response = AsyncMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"fields": fields}
    session = MagicMock()
    session.get.return_value.__aenter__.return_value = response
    assert asyncio.run(get_issue_fields(
        session, "https://jira/", {}, "ABC-1", ["customfield_10855", "fixVersions", "customfield_10855"],
    )) == fields
    assert session.get.call_args.args == ("https://jira/rest/api/3/issue/ABC-1",)
    assert session.get.call_args.kwargs["params"] == {
        "fields": "customfield_10855,fixVersions",
    }
    response.raise_for_status.assert_called_once()
    assert format_jira_field(fields.get("customfield_10855") or fields.get("fixVersions")) == (", ".join(expected) or "-")


class ReviewApp(App):
    def __init__(self, review):
        super().__init__()
        self.review = review

    def on_mount(self):
        self.push_screen(self.review)


def make_review(columns):
    return ReviewScreen(
        [{"key": "ABC-1", "summary": "First", "status": "In Progress", "assignee": "alice"},
         {"key": "ABC-2", "summary": "Second"}],
        {"ABC-1": "* Color Status: Green\nWork is progressing.", "ABC-2": "invalid"},
        {}, {}, None, "model", TokenUsage(), "https://jira", {},
        prefs=UserPrefs(columns=columns),
    )


@pytest.mark.parametrize("columns", [
    list(DEFAULT_COLUMNS),
    ["action", "target_version", "color", "summary", "key", "status", "assignee"],
    ["summary"],
])
def test_review_actions_with_configured_columns(monkeypatch, columns):
    lookup = AsyncMock(return_value={"customfield_10855": ["4.22", "4.23"]})
    monkeypatch.setattr("herald.app.get_issue_fields", lookup)

    async def run():
        review = make_review(columns)
        app = ReviewApp(review)
        async with app.run_test() as pilot:
            await pilot.pause()
            await review.workers.wait_for_complete()
            table = review.query_one(DataTable)
            assert [key.value for key in table.columns] == columns
            assert review._current_key() == "ABC-1"
            if "target_version" in columns:
                assert str(table.get_cell("ABC-1", "target_version")) == "4.22, 4.23"
                assert lookup.await_count == 2
                lookup.return_value = {}
                await review._load_jira_columns(["ABC-1"])
                assert str(table.get_cell("ABC-1", "target_version")) == "-"
            else:
                lookup.assert_not_awaited()
            if "assignee" in columns:
                assert table.get_cell("ABC-1", "assignee") == "alice"
                assert table.get_cell("ABC-2", "assignee") == "-"
            review.action_skip()
            assert review.results["ABC-1"].action == "skipped"
            assert review._current_key() == "ABC-2"
            review.action_approve()  # Invalid draft must be safe with color/action hidden.
            assert "ABC-2" not in review.results
            if "action" in columns:
                assert table.get_cell("ABC-1", "action") == "Skipped"
                assert table.get_cell("ABC-2", "action") == "Invalid draft"
            review._do_search("abc-1")  # Search by key even when that column is hidden.
            assert review._current_key() == "ABC-1"
            review.action_cursor_down()
            monkeypatch.setattr("herald.app.request_changes_api", AsyncMock(
                return_value="* Color Status: Yellow\nWaiting for approval.",
            ))
            await review._do_request_changes("ABC-2", "invalid", "", "Fix draft")
            if "color" in columns:
                assert table.get_cell("ABC-2", "color") == "Yellow"
            monkeypatch.setattr(review, "_start_jira_update", MagicMock())
            review.action_approve()
            review._start_jira_update.assert_called_once()
            assert review.results["ABC-2"].action == "approved"
            assert review._current_key() == "ABC-2"
    asyncio.run(run())


def test_version_lookup_failure_is_visible_and_recoverable(monkeypatch):
    lookup = AsyncMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr("herald.app.get_issue_fields", lookup)

    async def run():
        review = make_review(["target_version", "summary"])
        app = ReviewApp(review)
        async with app.run_test():
            await review.workers.wait_for_complete()
            table = review.query_one(DataTable)
            assert str(table.get_cell("ABC-1", "target_version")) == "Unavailable"
            lookup.side_effect = None
            lookup.return_value = {"customfield_10855": ["4.23"]}
            await review._load_jira_columns(["ABC-1"])
            assert str(table.get_cell("ABC-1", "target_version")) == "4.23"
            review.action_skip()
            assert review.results["ABC-1"].action == "skipped"
    asyncio.run(run())


@pytest.mark.parametrize("refresh_all", [False, True])
@pytest.mark.parametrize("columns", [["assignee", "action", "status", "target_version", "color"], ["summary"]])
def test_refresh_updates_configured_columns(monkeypatch, tmp_path, refresh_all, columns):
    lookup = AsyncMock(return_value={"customfield_10855": ["4.22"]})
    monkeypatch.setattr("herald.app.get_issue_fields", lookup)
    updated = {"key": "ABC-1", "summary": "Updated", "status": "Done", "assignee": "bob"}
    monkeypatch.setattr("herald.app.run_gather", AsyncMock(
        return_value=(tmp_path, {"issues": [updated, {"key": "ABC-2", "summary": "Second"}]}),
    ))
    draft = "* Color Status: Yellow\nWaiting for approval."
    monkeypatch.setattr("herald.app.draft_ryg", AsyncMock(return_value=draft))
    monkeypatch.setattr("herald.app.batch_draft", AsyncMock(return_value=(
        {"ABC-1": draft}, {}, [], {"ABC-1": "drafted"},
    )))
    monkeypatch.setattr("herald.app.save_draft_input", MagicMock())
    monkeypatch.setattr("herald.app._drafts_path", lambda _: tmp_path / "drafts.json")
    (tmp_path / "issues").mkdir()
    (tmp_path / "issues" / "ABC-1.json").write_text('{"issue": {"key": "ABC-1"}}')

    async def run():
        review = make_review(columns)
        monkeypatch.setattr(review, "_save_drafts", MagicMock())
        app = ReviewApp(review)
        async with app.run_test():
            await review.workers.wait_for_complete()
            lookup.return_value = {"customfield_10855": ["4.23"]}
            if refresh_all:
                await review._do_refresh_all()
            else:
                await review._do_refresh("ABC-1")
            await review.workers.wait_for_complete()
            table = review.query_one(DataTable)
            assert review.drafts["ABC-1"] == draft
            if "status" in columns:
                assert table.get_cell("ABC-1", "status") == "Done"
                assert table.get_cell("ABC-1", "assignee") == "bob"
                assert str(table.get_cell("ABC-1", "target_version")) == "4.23"
                assert table.get_cell("ABC-1", "color") == "Yellow"
                assert table.get_cell("ABC-1", "action") == "-"
            else:
                assert table.get_cell("ABC-1", "summary") == "Updated"
                lookup.assert_not_awaited()
    asyncio.run(run())


def test_custom_columns_from_prefs(tmp_path):
    path = tmp_path / "prefs.toml"
    path.write_text('''[tui]
columns = ["key", {label = "Priority", field = "priority.name"},
           {field = "customfield_12345"}, "action"]
''')
    prefs = load_prefs(path)
    assert [(column.key, column.label, column.field) for column in prefs.resolved_columns] == [
        ("key", "Key", None),
        ("jira:priority.name", "Priority", "priority.name"),
        ("jira:customfield_12345", "customfield_12345", "customfield_12345"),
        ("action", "Action", None),
    ]


@pytest.mark.parametrize("columns", [
    [{"label": "Missing field"}], [{"field": ""}], [{"field": 123}],
    [{"field": "priority..name"}], [{"field": "priority,*all"}],
    [{"field": "priority", "label": ""}], [{"field": "priority", "label": False}],
    [{"field": "priority", "typo": "Priority"}],
    [{"field": "priority"}, {"field": "priority", "label": "Again"}],
])
def test_invalid_custom_columns(columns):
    with pytest.raises(ValueError, match=r"\[tui\] columns"):
        UserPrefs(columns=columns)


@pytest.mark.parametrize(("reference", "expected"), [
    ("priority.name", "High"), ("components.name", "Networking, API"),
    ("assignee", "Alice"), ("customfield_12345", "0"),
    ("customfield_12346", "false"), ("missing.name", "-"),
    ("components.missing", "-"), ("labels", "alpha, [red]literal[/red]"),
    ("details", '{"nested":{"count":2}}'),
])
def test_custom_field_rendering(reference, expected):
    fields = {
        "priority": {"name": "High"},
        "components": [{"name": "Networking"}, {"name": "API"}],
        "assignee": {"displayName": "Alice", "name": "alice123"},
        "customfield_12345": 0, "customfield_12346": False,
        "labels": ["alpha", "[red]literal[/red]"],
        "details": {"nested": {"count": 2}},
    }
    assert format_jira_field(jira_field_value(fields, reference)) == expected


@pytest.mark.parametrize("with_target", [False, True])
def test_custom_columns_share_requests_and_refresh(monkeypatch, with_target):
    columns = [
        {"field": "priority.name", "label": "Priority"},
        {"field": "priority.id", "label": "Priority ID"},
        {"field": "components.name"}, {"field": "labels"},
        {"field": "customfield_12345"}, "action",
    ]
    if with_target:
        columns += ["target_version", {"field": "customfield_10855"}]
    lookup = AsyncMock(return_value={
        "priority": {"name": "High", "id": "1"},
        "components": [{"name": "API"}, {"name": "Networking"}],
        "labels": ["[red]literal[/red]"], "customfield_10855": [{"name": "4.22"}],
    })
    monkeypatch.setattr("herald.app.get_issue_fields", lookup)

    async def run():
        review = make_review(columns)
        async with ReviewApp(review).run_test():
            await review.workers.wait_for_complete()
            table = review.query_one(DataTable)
            assert lookup.await_count == 2  # One request per issue, regardless of column count.
            requested = ["priority", "components", "labels", "customfield_12345"]
            if with_target:
                requested += ["customfield_10855", "fixVersions"]
            assert lookup.call_args.args[-1] == requested
            assert str(table.get_cell("ABC-1", "jira:priority.name")) == "High"
            assert str(table.get_cell("ABC-1", "jira:priority.id")) == "1"
            assert str(table.get_cell("ABC-1", "jira:components.name")) == "API, Networking"
            assert str(table.get_cell("ABC-1", "jira:customfield_12345")) == "-"
            assert table.get_cell("ABC-1", "jira:labels").plain == "[red]literal[/red]"
            if with_target:
                assert str(table.get_cell("ABC-1", "target_version")) == "4.22"
                assert str(table.get_cell("ABC-1", "jira:customfield_10855")) == "4.22"
            lookup.side_effect = RuntimeError("offline")
            await review._load_jira_columns(["ABC-1"])
            assert str(table.get_cell("ABC-1", "jira:priority.name")) == "Unavailable"
            review.action_skip()
            assert table.get_cell("ABC-1", "action") == "Skipped"
            lookup.side_effect = None
            lookup.return_value = {"priority": {"name": "Low"}}
            await review._load_jira_columns(["ABC-1"])
            assert str(table.get_cell("ABC-1", "jira:priority.name")) == "Low"
    asyncio.run(run())
