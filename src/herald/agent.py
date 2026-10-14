"""Coding agent (Claude Code / pi / OpenCode) integration for interactive draft revision."""

import os
import subprocess as sp
import tempfile
from pathlib import Path
from typing import Optional

from .config import UserPrefs


def _resolve_coding_agent(prefs: UserPrefs) -> Optional[str]:
    """Return the coding agent CLI path from prefs, or None if unset/missing."""
    import shutil
    name = prefs.coding_agent
    if not name:
        return None
    return shutil.which(name)


def _build_agent_prompt(
    draft: str, issue_summary: str, instruction: str, tmppath: str,
    issue_key: Optional[str] = None, date_dir: Optional[Path] = None,
) -> str:
    """Build the prompt passed to the coding agent CLI."""
    data_context = ""
    if date_dir and issue_key:
        issue_json = date_dir / "issues" / f"{issue_key}.json"
        data_context = (
            f"Gathered Jira and GitHub data for this issue is at {issue_json}. "
            f"The full data directory is {date_dir}/issues/ — read the JSON "
            f"file to see PRs, changelog, comments, and descendant issues.\n"
        )
    return (
        f"I need you to revise a Jira R/Y/G status update.\n\n"
        f"The current draft is in {tmppath}.\n\n"
        f"Issue context:\n{issue_summary}\n\n"
        f"{data_context}\n"
        f"Requested change: {instruction}\n\n"
        f"Instructions:\n"
        f"1. Read {tmppath} to see the current draft.\n"
        f"2. Treat only dated events in `events_in_window` as evidence of "
        f"accomplishments from this reporting window. `carry_forward_context` "
        f"describes current or older state; it can support continuing blockers "
        f"but does not establish when an accomplishment happened.\n"
        f"3. A recent comment repeating an old accomplishment does not make that "
        f"accomplishment new. A PR's current state or historical merge date does "
        f"not make it a current-window merge. Report a recent review or commit "
        f"as that specific activity. Treat unknown dates or window metadata as "
        f"uncertainty; do not infer dates.\n"
        f"4. If the request involves PRs, commits, or code changes, read the "
        f"gathered data JSON above and/or use gh CLI to search GitHub.\n"
        f"5. Write the complete revised status update to {tmppath}, replacing "
        f"its entire contents. The output must keep the exact same format — "
        f"starting with '* Color Status:'. No preamble, no explanation, just "
        f"the status update.\n"
        f"6. Use the Jira Cloud `ryg_field_markdown` format: top-level bullets "
        f"are '* ', nested bullets are two spaces followed by '* ', and wiki-style "
        f"'**' sub-bullets are prohibited. Render PR references as Markdown links "
        f"labeled 'owner/repo#number' and use the PR URL and repository from the "
        f"evidence; do not output bare '#number' or shorthand 'repo#number', and "
        f"do not put raw PR URLs in parentheses. The URL belongs only in the "
        f"Markdown link target; never guess missing repository or URL details.\n"
    )


def _run_agent_interactive(
    agent: str, model: str, draft: str, issue_summary: str, instruction: str,
    issue_key: Optional[str] = None, date_dir: Optional[Path] = None,
) -> str:
    """Run the coding agent interactively (blocking, owns the terminal).

    Called inside app.suspend() so the agent gets full terminal control.
    """
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(draft)
        draft_path = f.name
    prompt = _build_agent_prompt(
        draft, issue_summary, instruction, draft_path,
        issue_key=issue_key, date_dir=date_dir,
    )
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(prompt)
        prompt_path = f.name
    short_prompt = f"Read your full instructions from {prompt_path} and follow them."
    agent_name = os.path.basename(agent)
    if agent_name == "opencode":
        cmd = [agent, "run", "-i", "--model", model, short_prompt]
    elif agent_name == "pi":
        cmd = [agent, "--model", model, f"@{prompt_path}"]
    else:
        cmd = [agent, "--model", model, short_prompt]
    try:
        sp.run(cmd)
    finally:
        os.unlink(prompt_path)
    try:
        with open(draft_path) as f:
            result = f.read().strip()
    finally:
        os.unlink(draft_path)
    return result if result else draft
