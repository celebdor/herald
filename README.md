# Herald

> From tickets to status — automated weekly feature reporting

Herald is a weekly-reporting copilot for engineering managers and leads. It
gathers what actually happened across your Jira issues and their GitHub PRs,
drafts Red/Yellow/Green status updates with an LLM, and lets you review, edit,
and push them back to Jira — all from the terminal. The report is the product:
you get a portfolio-wide R/Y/G rollup you'd otherwise hand-write every week,
grounded in real activity and shaped by your team's own reporting rules.

## Features

- **Automated data gathering** — pulls issue activity from Jira and GitHub (PRs, reviews, commits) for a configurable date range
- **LLM-drafted updates** — produces Red/Yellow/Green status summaries using your project's analysis and formatting rules
- **Interactive TUI review** — side-by-side view of the new draft and the previous status for each issue, so "what changed since last week" is front and center
- **Configurable columns** — choose and reorder built-in columns or display arbitrary Jira fields; use [`herald fields`](#finding-column-fields) to discover field references and inspect values
- **In-app editing** — edit drafts in `$EDITOR`, request AI revisions via the API, or hand off to a coding agent (Claude Code, pi, OpenCode) for tool-assisted changes
- **Single-issue refresh** — re-gather and re-draft one issue without restarting the pipeline
- **Same-day caching** — gathered data and drafts are cached per UTC day; subsequent runs skip re-gathering
- **Profiles** — save named parameter sets (project, component, label, assignees) in `prefs.toml` so each team reports with one command
- **Configurable reporting rules** — encode your org's reporting taste once (`writing_rules`) instead of re-explaining it every week
- **Cost tracking** — shows token usage and estimated cost at the end of each session

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Jira Cloud instance with API access
- An Anthropic API key or Google Cloud project with Vertex AI access
- A GitHub personal access token (for PR data)

## Installation

```bash
uv tool install --from . herald
```

Or for development:

```bash
uv sync
```

## Configuration

### Environment variables (required)

| Variable | Description |
|----------|-------------|
| `JIRA_USERNAME` | Atlassian account email |
| `JIRA_API_TOKEN` | [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `GITHUB_TOKEN` | GitHub personal access token |
| `ANTHROPIC_API_KEY` | Anthropic API key (direct API) |

For Vertex AI instead of the direct API, set `ANTHROPIC_VERTEX_PROJECT_ID` (and optionally `CLOUD_ML_REGION`, default `us-east5`).

The `herald fields` helper requires only the two Jira variables above.

### Preferences file

Copy the default config and customize:

```bash
mkdir -p ~/.config/herald
cp prefs.default.toml ~/.config/herald/prefs.toml
```

Key settings:

```toml
[llm]
model = "claude-sonnet-5"
# coding_agent = "claude"   # enables tool-assisted revisions ("claude", "pi", or "opencode")
writing_rules = [
  "No fractions or ratios like 7/8 or 4/25",
  "No completion percentages",
]

[jira]
url = "https://redhat.atlassian.net"
target_version_field = "customfield_10855"
# skills_dir = "~/src/ai-helpers/plugins/jira/skills/status-analysis"

[tui]
columns = ["key", "summary", "target_version", "color", "action"]

[profiles.my-team]
project = "MYPROJ"
component = "My Component"
label = "my-label"
exclude = ["bot@example.com"]
```

`[tui] columns` chooses the visible columns and their order. Built-in names
are `key`, `summary`, `target_version`, `color`, `action`, `status`, and `assignee`.
The list must be non-empty and contain no duplicate names or field references. Omitting
the setting uses the five columns shown above. Review actions and key/summary
search still work when their columns are hidden.

Add arbitrary Jira fields with inline tables, using a field ID and an optional
label (defaults to the field reference):

```toml
[tui]
columns = [
  "key",
  "summary",
  { label = "Priority", field = "priority.name" },
  { label = "Components", field = "components.name" },
  { label = "Team", field = "customfield_12345" },
  "target_version",
  "color",
  "action",
]
```

References are relative to the Jira issue's `fields` object; use API field IDs,
not Jira display names. Dotted paths select nested properties and traverse
arrays, as in `components.name`. Strings, numbers, and booleans display directly;
arrays display comma-separated values. Objects use their `displayName`, `name`,
`value`, or `key` when available, otherwise compact JSON. Empty or missing values
display `-` (including field IDs Jira omits because they are unknown or inaccessible).
All requested fields load together in one request per issue in the background
when review opens and on `r`/`R`. Failed requests show `Unavailable` and a warning.

Each row represents a Jira issue. **Target version** shows its Jira release
target, using `[jira] target_version_field` (Red Hat's `customfield_10855` by
default), with `fixVersions` as a fallback when empty. Set the field to
`"fixVersions"` for Jira installations that use only fix versions. Multiple
versions appear comma-separated. Values load in the background when review
opens and reload on `r`/`R`, including when gathered data is cached. `-` means
no version is set; `Unavailable` means the lookup failed. Hiding this column and
all custom field columns disables the extra Jira requests.

### Finding column fields

Use `herald fields` to find field references without starting the TUI:

```bash
# Search field names or IDs (case-insensitive); omit the query to list all
herald fields "target version"

# Inspect values on an issue, or search for a known value
herald fields "target version" --issue OCPSTRAT-2666
herald fields "4.22" --issue OCPSTRAT-2666

# Get a copyable prefs entry for a field, or inspect its full JSON value
herald fields customfield_10855 --issue OCPSTRAT-2666
herald fields assignee --issue OCPSTRAT-2666 --json
```

Results show field references, names, types, and optional value previews. A
single match includes an entry to paste into `[tui] columns`. Use `--json` to
inspect full objects and choose nested paths such as `assignee.displayName`.
JSON results include `present` and `value` when inspecting an issue; an empty
value is distinct from a field that Jira did not return. No matches produces
an empty JSON array, or a message in normal output.

This command requires only `JIRA_USERNAME` and `JIRA_API_TOKEN`, and uses
`[jira] url` from prefs. Override the prefs file with
`herald fields --prefs /path/to/prefs.toml`. It uses the
[Jira field catalog](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-fields/#api-rest-api-3-field-get),
so results depend on the fields Jira exposes to your account. With `--issue`,
searches also include that issue's raw field values.

For example, to add a Priority column:

1. Run `herald fields priority --issue OCPSTRAT-2666` with an issue you can access.
2. Copy the returned column entry into the existing `[tui] columns` array in
   `~/.config/herald/prefs.toml`. For Jira's standard priority field, that entry is
   `{ label = "Priority", field = "priority" }`.
3. Place it where you want the column to appear, then start Herald again:

```toml
[tui]
columns = [
  "key",
  "summary",
  { label = "Priority", field = "priority" },
  "target_version",
  "color",
  "action",
]
```

The helper prints configuration suggestions; it does not edit your preferences.
An exact field ID takes precedence over substring matches. Custom field IDs
vary between Jira installations, so use the IDs returned by your own instance.

### Reporting rules (status-analysis skill)

Herald's R/Y/G analysis and output format come from the `status-analysis`
skill in [openshift-eng/ai-helpers](https://github.com/openshift-eng/ai-helpers).
It is resolved in this order:

1. `[jira] skills_dir` in `prefs.toml` (explicit override)
2. `HERALD_SKILLS_DIR` environment variable
3. A previously auto-fetched copy in the platform data directory
4. A repo-relative path (when running from a source checkout)
5. Auto `git clone` of `openshift-eng/ai-helpers` into the data directory

Point `skills_dir` at your own fork or internal copy to standardize reporting
rules across teams.

## Usage

```bash
# Basic usage
herald --project OCPSTRAT --component "Hosted Control Planes"

# Using a saved profile
herald --project OCPSTRAT --profile my-team

# Force re-gather (ignore cache)
herald --project OCPSTRAT --refresh

# Regenerate drafts while reusing gathered data
herald --project OCPSTRAT --redraft

# Skip issues with no recent activity
herald --project OCPSTRAT --skip-quiet

# Custom date range
herald --project OCPSTRAT --start-date 2026-08-01 --end-date 2026-08-15

# Discover fields for custom review columns
herald fields "target version" --issue OCPSTRAT-2666
```

Gathered Jira/GitHub evidence defaults to `.work/weekly-status` and can be
changed with `--output-dir`. Drafts and their input fingerprints are stored
separately under the platform state directory (`$XDG_STATE_HOME/herald`
or `~/.local/state/herald` on Linux). `--redraft` skips saved drafts while
allowing Herald to reuse the gathered-data cache; combine it with `--refresh`
only when both evidence and drafts should be regenerated.

## TUI keybindings

The app has two phases: a pipeline screen (gather + draft) and the review screen.

### Review screen

| Key | Action |
|-----|--------|
| `a` | Approve draft and push to Jira |
| `s` | Skip issue |
| `e` | Edit draft in `$EDITOR` |
| `i` | Request AI revision (type instruction, press Enter) |
| `r` | Refresh current issue (re-gather + re-draft) |
| `R` | Refresh all issues (re-gather + re-draft) |
| `q` | Quit review |

When `coding_agent` is configured in prefs, `i` suspends the TUI and opens the agent interactively. Otherwise it uses a single API call. Supported agents: `claude`, `pi`, `opencode`.

## Running tests

```bash
uv run pytest tests/ -v
```

## Design specifications

See [specs/](specs/README.md) for the exact initial drafting API inputs and
the data flows for gathering, caching, review, revision, and Jira publication.

## Project structure

```
src/herald/
├── cli.py        # Argument parsing and entry point
├── config.py     # UserPrefs, path resolution, caching
├── fields.py     # Field discovery, value previews, column config suggestions
├── llm.py        # LLM client, cost tracking, drafting, system prompt
├── gather.py     # Data gathering subprocess, issue processing, batch drafting
├── jira.py       # Jira API: auth, ADF conversion, field updates
├── agent.py      # Coding agent (Claude Code / OpenCode) integration
└── app.py        # Textual TUI: ReviewScreen and StatusUpdateApp
```
