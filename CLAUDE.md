# Agent guidance for herald

Guidance for AI coding agents (Claude Code and others) working in this
repository.

## Commit messages — Conventional Commits (required)

Every commit you create **must** follow the
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)
specification — `<type>[optional scope]: <description>`, with an optional body
and footers. Use the imperative mood, keep the subject ≤72 characters, and flag
breaking changes with `!` and/or a `BREAKING CHANGE:` footer.

Allowed types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`,
`ci`, `chore`, `revert`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full
table, rules, and examples.

## Working in this repo

- Run the tests before and after changes: `python -m pytest`.
- Keep user-facing config and CLI behavior documented in both
  `prefs.default.toml` and `README.md`.
- Project layout: `src/herald/cli.py` (argument parsing + entry point),
  `src/herald/config.py` (`UserPrefs`, prefs loading, cache, path resolution),
  `src/herald/app.py` (the interactive app), `src/herald/gather.py`
  (Jira/GitHub data gathering), `tests/` (pytest suite).
