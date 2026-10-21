"""CLI entry point: argument parsing and main."""

import argparse
import asyncio
from pathlib import Path

import aiohttp

from .app import StatusUpdateApp
from .config import find_scripts_dir, load_prefs
from .fields import show_fields


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jira weekly status update tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  herald --project OCPSTRAT --component "Hosted Control Planes"
  herald --project OCPSTRAT --profile ocpstrat-hcp
  herald fields "target version" --issue OCPSTRAT-2666

Environment variables:
  JIRA_USERNAME            Atlassian account email (required)
  JIRA_API_TOKEN           Atlassian API token (required)
  GITHUB_TOKEN             GitHub personal access token (required)
  ANTHROPIC_API_KEY        Direct Anthropic API key
  ANTHROPIC_VERTEX_PROJECT_ID  Use Vertex AI instead of direct API
""",
    )
    parser.add_argument("--project", help="Jira project key (required for reporting, e.g. OCPSTRAT)")
    parser.add_argument("--component", help="Filter by component")
    parser.add_argument("--label", help="Filter by label")
    parser.add_argument("--assignee", action="append", help="Filter by assignee email (repeatable)")
    parser.add_argument("--exclude", action="append", help="Exclude assignee email (repeatable)")
    parser.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    parser.add_argument("--start-date", dest="start_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", dest="end_date", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--output-dir", dest="output_dir", default=".work/weekly-status",
        help="Output directory for gathered data (default: .work/weekly-status)",
    )
    parser.add_argument(
        "--skip-quiet", dest="skip_quiet", action="store_true",
        help="Skip issues with no significant activity",
    )
    parser.add_argument("--model", help="Anthropic model (default: from prefs or claude-sonnet-5)")
    parser.add_argument("--profile", help="Named profile from prefs.toml")
    parser.add_argument("--prefs", type=Path, help="Path to prefs.toml")
    parser.add_argument("--refresh", action="store_true", help="Force re-gather even if cached data exists")
    parser.add_argument(
        "--redraft", action="store_true",
        help="Regenerate drafts while reusing gathered data when available",
    )
    commands = parser.add_subparsers(dest="command")
    fields = commands.add_parser("fields", help="Find Jira field references for TUI columns")
    fields.add_argument("query", nargs="?", default="", help="Search field names, IDs, or issue values (case-insensitive)")
    fields.add_argument("--issue", help="Issue key whose field values to inspect")
    fields.add_argument("--json", action="store_true", help="Print JSON with full field values")
    fields.add_argument("--prefs", type=Path, default=argparse.SUPPRESS, help="Path to prefs.toml")
    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None and not args.project:
        parser.error("--project is required for reporting")
    try:
        prefs = load_prefs(args.prefs)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    if args.command == "fields":
        try:
            await show_fields(args, prefs)
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            parser.exit(1, f"herald fields: {str(exc) or 'Jira request timed out'}\n")
        return

    if args.profile and args.profile in prefs.profiles:
        profile = prefs.profiles[args.profile]
        for key, value in profile.items():
            if not getattr(args, key, None):
                setattr(args, key, value)

    scripts_dir = find_scripts_dir(prefs)
    app = StatusUpdateApp(args, prefs, scripts_dir)
    await app.run_async()


def cli() -> None:
    asyncio.run(main())
