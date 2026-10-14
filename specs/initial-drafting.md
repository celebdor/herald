# Initial feature-status drafting contract

## Request boundary

For each uncached eligible issue, Herald creates a compact JSON object using
[`_prune_issue`](../src/herald/gather.py), serializes it with
`json.dumps(..., indent=2)`, and sends it as one user message through
[`draft_ryg`](../src/herald/llm.py). Calls are independent: an issue is not given
the other features' reports or a portfolio-wide conversation.

The application supplies these request arguments:

```python
await client.messages.create(
    model=model,
    max_tokens=2048,
    system=system_prompt,
    messages=[{"role": "user", "content": issue_summary}],
)
```

`issue_summary` is a JSON string, not an attached file. The model is selected by
`--model`, then `[llm].model`, then the configured code default. `max_tokens` is
an output limit; there is no explicit input truncation or token budgeting here.
No tools, temperature, conversation history, or structured-output schema are
passed by this call. There is no explicit `cache_control` in the request;
identical system prompts alone do not establish an explicit prompt-cache policy.

`create_client` selects Anthropic through Vertex AI when
`ANTHROPIC_VERTEX_PROJECT_ID` or `GOOGLE_CLOUD_PROJECT` is set, otherwise the
direct Anthropic client. The Vertex region is `CLOUD_ML_REGION`, defaulting to
`us-east5`. Credentials configure the clients and data sources; Herald does not
add credential values to the model prompt.

## Temporal evidence contract

The gatherer manifest is the authority for the resolved reporting interval and
must provide:

```json
{
  "reporting_window": {
    "start": "2026-10-07T22:00:00Z",
    "end_exclusive": "2026-10-14T22:00:00Z",
    "timezone": "Europe/Prague"
  },
  "gathered_at": "2026-10-14T12:34:56Z",
  "issue_gathered_at": {"DEMO-42": "2026-10-14T12:34:56Z"}
}
```

The interval is half-open: `start <= occurred_at < end_exclusive`. Date-only
CLI arguments are resolved by the gatherer in the named IANA timezone, then
converted to offset-aware instants. Herald does not infer a timezone or
reconstruct the interval from command-line defaults. If the window is missing,
invalid, or predates this contract, the prompt says it is unknown. A missing or
invalid `gathered_at` is represented as unknown freshness.

Each issue may provide `events_in_window`, `carry_forward_context`, and
`undated_activity`. An event has an event-specific `occurred_at` instant;
Herald includes it as current-window evidence only when its timestamp parses
and falls inside the resolved half-open interval. Dated events outside the
window move to `carry_forward_context.older_activity`; undated events move to
`undated_activity`. Legacy Jira comment and changelog entries are preserved
when they contain their own timestamp. PR state and aggregate commit/review
counts are never converted into dated events. The external gatherer must emit
individual merge, commit, review, comment, and Jira transition events with the
timestamp of the claimed action.

The payload does not yet assign stable evidence IDs or ask the model for
structured claim references. Those checks belong to the second implementation
stage.

## System message

[`build_system_prompt`](../src/herald/llm.py) builds the message in this order:

1. A role instruction: act as a Jira analyst producing weekly R/Y/G updates and
   apply the supplied analysis methodology and output format.
2. Temporal evidence rules for separating current-window events from
   carry-forward state and for treating unknown dates as uncertainty.
3. The full contents of `activity-analysis.md` from the resolved status-analysis
   directory, prefixed with its filename heading.
4. The full contents of `formatting.md` from that directory, similarly headed.
5. Every configured `[llm].writing_rules` entry, as an additional bullet rule.
6. An instruction to produce only the `ryg_field_markdown` format for Jira
   Cloud REST API v3, with no preamble or explanation and a first line starting
   with `* Color Status:`. Markdown
   top-level bullets must use `* ` and nested bullets `  * `; wiki-style `**`
   sub-bullets are prohibited because the Jira conversion parses Markdown. PR
   references must be Markdown links labeled `owner/repo#number`, with no raw
   PR URLs in parentheses.

The directory resolution order is preferences `skills_dir`,
`HERALD_SKILLS_DIR`, previously fetched platform data, a source-tree-relative
checkout, then an automatic clone of `openshift-eng/ai-helpers`. Missing
analysis/formatting files are currently silently omitted. Their exact contents
are deployment-specific and are not bundled in this repository.

Writing rules come from loaded preferences. The examples in
[`prefs.default.toml`](../prefs.default.toml) apply only if configured; missing
preferences default to an empty writing-rules list.

## User message: complete field mapping

The source is the gathered `issues/<key>.json`. `issue` means its `issue`
member, falling back to the whole object when that member is absent.

| Sent field | Gathered source / transformation |
| --- | --- |
| `key` | `issue.key` |
| `summary` | `issue.summary` (the issue title, not its full description) |
| `status` | `issue.status`, forwarded as provided |
| `assignee` | `issue.assignee.name` for an object; otherwise the provided value; missing/empty becomes `"?"` |
| `reporting_window` | Resolved manifest window, with `status: known` only for valid offset-aware start/end and IANA timezone; otherwise values are null and status is `unknown` |
| `gathered_at` / `gather_freshness` | Valid per-issue timestamp from `issue_gathered_at`, otherwise a valid full-gather timestamp when no per-issue map exists; legacy or invalid values become null / `unknown` |
| `events_in_window[]` | Gatherer events with valid timestamps inside the half-open window; legacy dated Jira comments/transitions are adapted here |
| `undated_activity[]` | Gatherer coverage gaps plus legacy Jira comments/transitions without an event timestamp |
| `carry_forward_context.current_status_summary` | `issue.current_status_summary`, converted from ADF to Markdown when possible; missing becomes `""` |
| `carry_forward_context.last_status_update` | `issue.last_status_summary_update`; absent becomes `""` |
| `carry_forward_context.descendants` | Current total, rounded completion, and status counts; no activity dates are inferred |
| `carry_forward_context.pull_requests[]` | Current PR number, repository, URL, title, state, draft flag, merge date, and dates object; these describe state and do not establish an in-window event |
| `carry_forward_context.older_activity[]` | Dated events outside the reporting interval |

Legacy source comment and changelog buckets are used only when
`events_in_window` is absent. Entries with truthy `is_bot` are excluded.
Herald validates timestamps and classifies dated records against the manifest
window. PR event detail must come from the gatherer; aggregate counts are not
evidence. Lists are not capped by Herald. `_field_to_text` falls back to JSON
text when ADF conversion fails; non-object values become strings.

### Illustrative payload

This is synthetic data showing the exact output keys. Nested date/descendant
contents depend on the external gatherer.

```json
{
  "key": "DEMO-42",
  "summary": "Add regional failover",
  "status": "In Progress",
  "assignee": "Example Lead",
  "reporting_window": {
    "start": "2026-10-07T22:00:00Z",
    "end_exclusive": "2026-10-14T22:00:00Z",
    "timezone": "Europe/Prague",
    "status": "known"
  },
  "gathered_at": "2026-10-14T12:34:56Z",
  "gather_freshness": "known",
  "events_in_window": [{
    "type": "jira_comment",
    "occurred_at": "2026-10-12T10:00:00Z",
    "author": "Example Lead",
    "text": "Validation is delayed while the test environment is unavailable."
  }],
  "undated_activity": [],
  "carry_forward_context": {
    "current_status_summary": "* Color Status: Green\n* Status summary: Implementation underway.",
    "last_status_update": "2026-10-07T09:00:00Z",
    "status": "In Progress",
    "descendants": {
      "total": 4,
      "completion_pct": 50,
      "by_status": {"Done": 2, "In Progress": 2}
    },
    "pull_requests": [{
      "number": 123,
      "repository": "openshift/hypershift",
      "url": "https://github.com/openshift/hypershift/pull/123",
      "title": "Implement regional failover",
      "state": "OPEN",
      "is_draft": false,
      "merged_at": null,
      "dates": {}
    }],
    "older_activity": []
  }
}
```

## Information not explicitly supplied

The payload does not include the full manifest, current date,
project/component/label filters, issue
description, explicit delivery targets/dependency fields, commit messages/diffs,
individual review text, or the full
gathered JSON. Some facts or links can still occur inside free-text comments,
previous status, or carry-forward context; omission of a dedicated field is
not a guarantee that the information is absent from that text.

There is no model tool access to Jira, GitHub, the filesystem, or source code
during initial drafting. Any color reasoning is expressed only through the
requested report format; Herald does not separately capture a rationale or
claim-to-source citation map. Unknown temporal coverage is explicit, while
some other missing values may still default to empty containers.

The completion percentage is supplied to the model even if writing rules
prohibit including percentages in the resulting report.

## Batch and response behavior

[`batch_draft`](../src/herald/gather.py) permits up to ten issue jobs at a time.
A drafting exception is retried up to three total attempts with one- and
two-second delays. Successful SDK responses update token usage. `_first_text`
returns the first text block; if none exists it currently falls back to a
thinking block, otherwise raises. There is no stop-reason/truncation check.

Drafts are saved under the platform state directory, scoped by output directory
and report date; `draft_inputs.json` stores a SHA-256 fingerprint of the exact
pruned input for each draft. A saved draft is reused
only when its input fingerprint matches the current temporal and evidence
payload. Pruned summaries are rebuilt for cached drafts so revisions retain
their evidence context. Legacy drafts without fingerprints are regenerated.
`--redraft` skips saved drafts while still allowing reuse of gathered data.
See [cache flows](data-flows.md#caching).

At this baseline there is no output schema validation and unrecognized color
defaults to Green. [PR #8](https://github.com/celebdor/herald/pull/8) proposes
explicit validation and Unknown status, plus distinct per-issue drafting
outcomes. Update this section when that contract ships.

## Design follow-ups

- [Issue #5](https://github.com/celebdor/herald/issues/5): preserve revision
  context and make cache identity/freshness reliable.
- [Issue #6](https://github.com/celebdor/herald/issues/6): an optional Evidence
  pane using saved source data, including source links, dates, gaps, and a
  supported explanation of the proposed color. Displaying raw evidence does
  not by itself prove which evidence the model used.
