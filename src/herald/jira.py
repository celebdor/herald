"""Jira API interaction: authentication, ADF conversion, and field updates."""

import base64
import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import aiohttp
import pyadf


def _issue_url(jira_url: str, issue_key: str) -> str:
    """Build an issue API URL; the quoted key stays a single path segment."""
    return f"{jira_url.rstrip('/')}/rest/api/3/issue/{quote(issue_key, safe='')}"


def _text_to_adf(text: str) -> dict:
    """Convert markdown text into an ADF document for Jira Cloud API v3."""
    return pyadf.Document.from_markdown(text).to_adf()


@dataclass
class JiraUpdateResult:
    ok: bool
    error: Optional[str] = None
    state: str = "published"  # "published", "rejected", or "unconfirmed"


def _normalized_adf(value):
    """Canonicalize ADF for comparing content despite JSON/mark ordering."""
    if isinstance(value, list):
        return [_normalized_adf(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in sorted(value.items()):
            if key == "version":
                continue
            value_normalized = _normalized_adf(item)
            # Marks have no ordering semantics; content arrays do.
            if key == "marks" and isinstance(value_normalized, list):
                value_normalized = sorted(
                    value_normalized, key=lambda mark: json.dumps(mark, sort_keys=True)
                )
            normalized[key] = value_normalized
        return normalized
    return value


async def get_jira_fields(
    session: aiohttp.ClientSession, jira_url: str, auth_headers: dict,
) -> list[dict]:
    """Return the system and custom field catalog visible to this user."""
    async with session.get(
        f"{jira_url.rstrip('/')}/rest/api/3/field",
        headers=auth_headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def get_issue_fields(
    session: aiohttp.ClientSession,
    jira_url: str,
    auth_headers: dict,
    issue_key: str,
    field_ids: list[str],
) -> dict:
    """Read all requested display fields in one request per issue."""
    async with session.get(
        _issue_url(jira_url, issue_key),
        params={"fields": ",".join(dict.fromkeys(field_ids))},
        headers=auth_headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return (await resp.json())["fields"]


def jira_field_value(fields: dict, reference: str):
    """Resolve a field path relative to Jira's fields object, including lists."""
    def descend(value, parts):
        if not parts:
            return value
        if isinstance(value, list):
            return [descend(item, parts) for item in value]
        if isinstance(value, dict):
            return descend(value.get(parts[0]), parts[1:])
        return None

    return descend(fields, reference.split("."))


def format_jira_field(value) -> str:
    """Render Jira scalars, arrays and objects as a single table cell."""
    def render(item):
        if item is None:
            return ""
        if isinstance(item, str):
            return " ".join(item.split())
        if isinstance(item, bool):
            return str(item).lower()
        if isinstance(item, list):
            return ", ".join(dict.fromkeys(text for child in item if (text := render(child))))
        if isinstance(item, dict):
            if not item:
                return ""
            for key in ("displayName", "name", "value", "key"):
                if key in item and item[key] is not None:
                    return render(item[key])
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        return str(item)

    return render(value) or "-"


async def update_jira_status(
    session: aiohttp.ClientSession,
    jira_url: str,
    auth_headers: dict,
    issue_key: str,
    status_text: str,
) -> JiraUpdateResult:
    """Set customfield_10814 on a Jira issue.

    After a successful PUT, reads the field back to verify it actually changed.
    """
    url = _issue_url(jira_url, issue_key)
    payload = {"fields": {"customfield_10814": _text_to_adf(status_text)}}
    try:
        async with session.put(url, json=payload, headers=auth_headers) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                if resp.status == 403:
                    return JiraUpdateResult(
                        False,
                        f"No permission to update {issue_key} (customfield_10814). "
                        f"Check your API token has edit access.",
                        "rejected",
                    )
                return JiraUpdateResult(
                    False, f"Failed to update {issue_key}: {resp.status} {text}", "rejected"
                )
    except Exception as exc:
        return JiraUpdateResult(
            False,
            f"Could not confirm whether the update for {issue_key} reached Jira: {exc}",
            "unconfirmed",
        )

    verify_url = _issue_url(jira_url, issue_key)
    try:
        async with session.get(
            verify_url, headers=auth_headers, params={"fields": "customfield_10814"},
        ) as resp:
            if resp.status not in (200, 204):
                return JiraUpdateResult(
                    False,
                    f"PUT for {issue_key} returned 204 but verify GET failed "
                    f"({resp.status}). Update may not have persisted.",
                    "unconfirmed",
                )
            data = await resp.json()
            actual = data.get("fields", {}).get("customfield_10814")
            if actual is None:
                return JiraUpdateResult(
                    False,
                    f"{issue_key} customfield_10814 is null after PUT. The field "
                    f"may not exist on this issue type or was silently rejected.",
                    "unconfirmed",
                )

            expected = _normalized_adf(payload["fields"]["customfield_10814"])
            if _normalized_adf(actual) != expected:
                return JiraUpdateResult(
                    False,
                    f"Jira accepted the update for {issue_key}, but read-back content "
                    "does not match. Publication is unconfirmed; retry after checking Jira.",
                    "unconfirmed",
                )
    except Exception as exc:
        return JiraUpdateResult(
            False,
            f"PUT for {issue_key} succeeded, but read-back failed: {exc}",
            "unconfirmed",
        )

    return JiraUpdateResult(True)


def get_jira_auth() -> dict:
    """Build Jira auth headers from JIRA_USERNAME + JIRA_API_TOKEN env vars."""
    username = os.environ.get("JIRA_USERNAME", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not username or not token:
        raise ValueError("JIRA_USERNAME and JIRA_API_TOKEN must be set")
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
