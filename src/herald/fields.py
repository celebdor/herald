"""Field discovery for configuring Jira-backed review columns."""

import json

import aiohttp
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .jira import format_jira_field, get_issue_fields, get_jira_auth, get_jira_fields


def field_matches(catalog: list[dict], values: dict | None, query: str) -> list[dict]:
    """Search names, IDs, and raw values without conflating missing and empty."""
    by_id = {field["id"]: field for field in catalog}
    # An issue may expose fields that the catalog doesn't list.
    for field_id in values or {}:
        by_id.setdefault(field_id, {"id": field_id, "name": field_id})
    matches = []
    query = query.casefold()
    exact_ids = {key: field for key, field in by_id.items() if key.casefold() == query}
    if exact_ids:
        by_id = exact_ids
    for field_id, field in by_id.items():
        schema = field.get("schema") or {}
        field_type = schema.get("type", "unknown")
        if field_type == "array":
            field_type = f"array<{schema.get('items', 'unknown')}>"
        name = field.get("name") or field_id
        row = {"field": field_id, "name": name, "type": field_type}
        searchable = [field_id, name]
        if values is not None:
            row["present"] = field_id in values
            row["value"] = values.get(field_id)
            if row["present"]:
                searchable.append(json.dumps(row["value"], ensure_ascii=False))
        if any(query in text.casefold() for text in searchable):
            matches.append(row)
    return sorted(matches, key=lambda row: (row["name"].casefold(), row["field"]))


def column_entry(row: dict) -> str:
    # JSON string quoting is also valid in TOML basic strings.
    return '{ label = ' + json.dumps(row["name"], ensure_ascii=False) + ', field = ' + json.dumps(row["field"]) + ' }'


async def show_fields(args, prefs) -> None:
    """List fields and optionally inspect an issue; no gathering or LLM needed."""
    headers = get_jira_auth()
    async with aiohttp.ClientSession() as session:
        catalog = await get_jira_fields(session, prefs.jira_url, headers)
        values = None
        if args.issue:
            values = await get_issue_fields(session, prefs.jira_url, headers, args.issue, ["*all"])
    matches = field_matches(catalog, values, args.query)
    if args.json:
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return
    console = Console()
    if not matches:
        console.print(Text(f"No Jira fields matched {args.query!r}. Try a shorter name or field ID."))
        return
    table = Table(title=f"Jira fields — {args.issue}" if args.issue else "Jira fields")
    table.add_column("Field reference", overflow="fold")
    table.add_column("Name", overflow="fold")
    table.add_column("Type")
    if args.issue:
        table.add_column("Value", overflow="fold")
    for row in matches:
        cells = [row["field"], row["name"], row["type"]]
        if args.issue:
            value = format_jira_field(row["value"]) if row["present"] else "(not returned)"
            cells.append(value if len(value) <= 100 else value[:99] + "…")
        table.add_row(*(Text(cell) for cell in cells))
    console.print(table)
    if args.issue:
        console.print(Text("Values are previews; use --json for full objects and nested properties. "
                           "'-' means empty; '(not returned)' means Jira omitted the field."))
    else:
        console.print(Text("Add --issue ISSUE-123 to inspect values or search for a known value."))
    if len(matches) == 1:
        # soft_wrap keeps the TOML entry on one line so it can be copied whole.
        console.print(
            Text("Add this entry to [tui] columns in prefs.toml:\n" + column_entry(matches[0])),
            soft_wrap=True,
        )
    else:
        console.print(Text("Search an exact field ID to get a column entry for prefs.toml."))
