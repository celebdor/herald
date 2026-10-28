"""Offline tests for Jira column field discovery and CLI routing."""

import asyncio
import importlib
import json
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from herald.config import UserPrefs
from herald.fields import column_entry, field_matches, show_fields
from herald.jira import get_jira_fields

cli = importlib.import_module("herald.cli")

CATALOG = [
    {"id": "customfield_10855", "name": "Target Version", "schema": {"type": "array", "items": "version"}},
    {"id": "customfield_108550", "name": "Other version", "schema": {"type": "string"}},
    {"id": "assignee", "name": "Assignee", "schema": {"type": "user"}},
    {"id": "flag", "name": "Flag"},
]
VALUES = {"customfield_10855": [{"name": "4.22"}], "assignee": None, "flag": False, "extra": 0}


def test_search_names_ids_and_values():
    assert field_matches(CATALOG, None, "TARGET")[0]["field"] == "customfield_10855"
    assert len(field_matches(CATALOG, None, "customfield_10855")) == 1
    row = field_matches(CATALOG, VALUES, "4.22")[0]
    assert row["type"] == "array<version>"
    assert row["value"] == [{"name": "4.22"}]
    assert row["present"] is True
    assert field_matches(CATALOG, VALUES, "false")[0]["field"] == "flag"
    assert field_matches(CATALOG, VALUES, "extra")[0]["value"] == 0
    assert field_matches(CATALOG, VALUES, "unmatched") == []


def test_missing_and_empty_are_distinct():
    rows = {row["field"]: row for row in field_matches(CATALOG, VALUES, "")}
    assert rows["assignee"]["present"] is True
    assert rows["assignee"]["value"] is None
    assert rows["customfield_108550"]["present"] is False
    assert rows["customfield_108550"]["value"] is None
    assert "value" not in field_matches(CATALOG, None, "assignee")[0]


def test_column_entry_is_valid_toml():
    row = {"name": 'Team "A"\\B\nC', "field": "customfield_12345"}
    data = tomllib.loads("[tui]\ncolumns = [" + column_entry(row) + "]")
    prefs = UserPrefs(columns=data["tui"]["columns"])
    assert prefs.resolved_columns[0].label == row["name"]
    assert prefs.resolved_columns[0].field == row["field"]


def test_field_catalog_request():
    response = AsyncMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = CATALOG
    session = MagicMock()
    session.get.return_value.__aenter__.return_value = response
    assert asyncio.run(get_jira_fields(session, "https://jira/", {"Accept": "application/json"})) == CATALOG
    assert session.get.call_args.args == ("https://jira/rest/api/3/field",)
    response.raise_for_status.assert_called_once()


@pytest.mark.parametrize("issue", [None, "ABC-1"])
def test_fields_output(monkeypatch, capsys, issue):
    monkeypatch.setattr("herald.fields.get_jira_auth", lambda: {})
    monkeypatch.setattr("herald.fields.get_jira_fields", AsyncMock(return_value=CATALOG))
    values = AsyncMock(return_value=VALUES)
    monkeypatch.setattr("herald.fields.get_issue_fields", values)
    args = SimpleNamespace(query="customfield_10855", issue=issue, json=False)
    asyncio.run(show_fields(args, UserPrefs()))
    output = capsys.readouterr().out
    assert 'field = "customfield_10855"' in output
    if issue:
        assert "4.22" in output
        assert values.call_args.args[-1] == ["*all"]
    else:
        values.assert_not_awaited()
    args.json = True
    asyncio.run(show_fields(args, UserPrefs()))
    output = json.loads(capsys.readouterr().out)
    assert output[0]["field"] == "customfield_10855"
    assert ("value" in output[0]) == bool(issue)
    args.query = "no-such-field"
    asyncio.run(show_fields(args, UserPrefs()))
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("argv", [
    ["fields", "version", "--prefs", "prefs.toml"],
    ["--prefs", "prefs.toml", "fields", "version"],
])
def test_field_parser_prefs_position(argv):
    args = cli.build_parser().parse_args(argv)
    assert str(args.prefs) == "prefs.toml"
    assert args.command == "fields"
    assert args.query == "version"


def test_fields_does_not_start_report(monkeypatch):
    monkeypatch.setattr("sys.argv", ["herald", "fields", "target"])
    monkeypatch.setattr(cli, "load_prefs", lambda _: UserPrefs())
    handler = AsyncMock()
    monkeypatch.setattr(cli, "show_fields", handler)
    scripts = MagicMock(side_effect=AssertionError("must not resolve scripts"))
    app = MagicMock(side_effect=AssertionError("must not start TUI"))
    monkeypatch.setattr(cli, "find_scripts_dir", scripts)
    monkeypatch.setattr(cli, "StatusUpdateApp", app)
    asyncio.run(cli.main())
    handler.assert_awaited_once()
    scripts.assert_not_called()
    app.assert_not_called()


def test_reporting_still_requires_project(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["herald"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli.main())
    assert exc.value.code == 2
    assert "--project is required" in capsys.readouterr().err


def test_fields_rejects_invalid_issue_key(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["herald", "fields", "version", "--issue", "ABC-1?fields=*all"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli.main())
    assert exc.value.code == 2
    assert "--issue must be a Jira issue key" in capsys.readouterr().err


def test_reporting_command_unchanged(monkeypatch):
    monkeypatch.setattr("sys.argv", ["herald", "--project", "ABC"])
    monkeypatch.setattr(cli, "load_prefs", lambda _: UserPrefs())
    monkeypatch.setattr(cli, "find_scripts_dir", lambda _: "scripts")
    app = MagicMock()
    app.return_value.run_async = AsyncMock()
    monkeypatch.setattr(cli, "StatusUpdateApp", app)
    asyncio.run(cli.main())
    assert app.call_args.args[0].project == "ABC"
    app.return_value.run_async.assert_awaited_once()


@pytest.mark.parametrize("error", [ValueError("missing credentials"), aiohttp.ClientError("offline"), asyncio.TimeoutError()])
def test_fields_errors_are_concise(monkeypatch, capsys, error):
    monkeypatch.setattr("sys.argv", ["herald", "fields"])
    monkeypatch.setattr(cli, "load_prefs", lambda _: UserPrefs())
    monkeypatch.setattr(cli, "show_fields", AsyncMock(side_effect=error))
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli.main())
    assert exc.value.code == 1
    assert "herald fields:" in capsys.readouterr().err
