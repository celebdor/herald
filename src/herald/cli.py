"""CLI entry point: argument parsing and main."""

import argparse
import asyncio
from pathlib import Path
from typing import Optional

from .app import StatusUpdateApp
from .config import find_scripts_dir, load_prefs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jira weekly status update tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  herald --project OCPSTRAT --component "Hosted Control Planes"
  herald --project OCPSTRAT --profile ocpstrat-hcp

Environment variables:
  JIRA_USERNAME            Atlassian account email (required)
  JIRA_API_TOKEN           Atlassian API token (required)
  GITHUB_TOKEN             GitHub personal access token (required)
  ANTHROPIC_API_KEY        Direct Anthropic API key
  ANTHROPIC_VERTEX_PROJECT_ID  Use Vertex AI instead of direct API
""",
    )
    parser.add_argument("--project", help="Jira project key (e.g. OCPSTRAT); may come from --profile")
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
    return parser


def _explicit_args(argv: Optional[list[str]]) -> set[str]:
    """Names of the options actually passed on the command line.

    argparse can't tell "not passed" from "passed with the default value", so
    parse once more with every default suppressed and see what was set.
    """
    parser = build_parser()
    for action in parser._actions:
        action.default = argparse.SUPPRESS
    return set(vars(parser.parse_args(argv)))


def apply_profile(
    parser: argparse.ArgumentParser, args: argparse.Namespace, prefs, explicit: set[str],
) -> None:
    """Fill args the user didn't pass from the named profile, and require a project.

    Profile keys are option names with `-` or `_` (`start-date`, `start_date`).
    An unknown key is an error, since it would otherwise be silently ignored.
    """
    if args.profile:
        if args.profile not in prefs.profiles:
            parser.error(f"profile {args.profile!r} not found in prefs.toml")
        known = {action.dest for action in parser._actions if action.dest != "help"}
        profile = {key.replace("-", "_"): value for key, value in prefs.profiles[args.profile].items()}
        unknown = sorted(set(profile) - known)
        if unknown:
            parser.error(
                f"profile {args.profile!r} has unknown option(s): {', '.join(unknown)} "
                f"(valid: {', '.join(sorted(known - {'profile', 'prefs'}))})"
            )
        for key, value in profile.items():
            if key not in explicit:
                setattr(args, key, value)
    if not args.project:
        parser.error("--project is required (or set project in the --profile)")


def resolve_args(argv: Optional[list[str]] = None, prefs=None):
    """Parse the command line and apply the selected profile. Returns (args, prefs)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if prefs is None:
        prefs = load_prefs(args.prefs)
    apply_profile(parser, args, prefs, _explicit_args(argv))
    return args, prefs


async def main() -> None:
    args, prefs = resolve_args()
    scripts_dir = find_scripts_dir(prefs)
    app = StatusUpdateApp(args, prefs, scripts_dir)
    await app.run_async()


def cli() -> None:
    asyncio.run(main())
