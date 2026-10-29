# Contributing to Herald

## Development setup

```bash
git clone https://github.com/celebdor/herald.git
cd herald
uv sync
```

Run the app from the checkout with `uv run herald` (see [README.md](README.md)
for configuration and usage).

## Running tests

```bash
uv run pytest tests/ -v
```

Note: if an editable install of Herald from another checkout exists on your
machine, it can shadow `src/`. If tests behave as if your changes aren't
there, check where the package resolves from:

```bash
uv run python -c "import herald.app; print(herald.app.__file__)"
```

and run against the checkout explicitly with `PYTHONPATH=src uv run pytest`.

## Re-running with an older gather cache

Herald caches gathered data per UTC day, so a normal run re-gathers once the
calendar rolls over. When iterating on the TUI or the drafting pipeline you
usually don't need fresh evidence — you can reuse a previous day's gather and
drafts to start the app in seconds without touching Jira, GitHub, or the LLM.

There are two caches (see [specs/data-flows.md](specs/data-flows.md) for the
full design):

1. **Gather cache** — `~/.cache/herald/<key>/cache_meta.json` (Linux; the
   platform cache directory on macOS/Windows). The key is a hash of the gather
   parameters (project, component, label, assignees, exclusions, days,
   explicit date range), and the entry is only considered fresh if the meta
   file's mtime falls on the current UTC day. The file stores the `date_dir`
   of the gathered data (`.work/weekly-status/<end-date>` by default).
2. **Draft cache** — `~/.local/state/herald/drafts/<id>/<date>/drafts.json`,
   reused automatically whenever the fingerprint of the pruned input for an
   issue is unchanged. Draft reuse has no expiry — only changed inputs
   invalidate it.

To re-run against an older gather, refresh the meta file's mtime:

```bash
# Find the entry pointing at the date you want
for d in ~/.cache/herald/*/; do echo "$d: $(cat "$d/cache_meta.json")"; done

# Mark it fresh for today
touch ~/.cache/herald/<key>/cache_meta.json
```

Then run with exactly the same arguments (or profile) as the original run —
the cache key hashes them, so different flags resolve to a different
directory. The pipeline will log `Using cached data from …`, every issue whose
input is unchanged will report `Loaded <KEY> (cached draft)`, and the review
screen appears without any network or LLM calls.

Caveats:

- Run from the same working directory as the original run. `date_dir` is
  stored relative to where Herald was launched, and the draft cache is keyed
  by the resolved output-directory path.
- The evidence is still from the original gather date — the reporting window
  and any temporal framing in drafts reflect that day, not today.
- Use `--redraft` if you want to regenerate drafts (LLM calls) while keeping
  the gathered data, and `--refresh` to re-gather the data itself.

## Testing a worktree checkout against existing data

When developing in a git worktree, `.work/` and the draft state usually live
in your main checkout. You can run the worktree's code against them by
launching from the main checkout root with the worktree's `src/` on
`PYTHONPATH`:

```bash
cd ~/code/<path>/herald                       # the main checkout, where .work/ lives
PYTHONPATH=.claude/worktrees/<name>/src uv run python -m herald <args>
```

Herald resolves the gather output and draft state relative to the current
directory, so this reuses the main checkout's caches while executing the
worktree's code.
