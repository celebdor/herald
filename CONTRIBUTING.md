# Contributing to herald

Thanks for contributing! Please read the guidelines below before opening a
pull request.

## Commit messages — Conventional Commits (required)

All commits **must** follow the
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)
specification. This keeps history readable and lets tooling derive changelogs
and version bumps.

Format:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

Allowed `type` values:

| Type       | Use for                                                        |
| ---------- | -------------------------------------------------------------- |
| `feat`     | A new feature                                                  |
| `fix`      | A bug fix                                                      |
| `docs`     | Documentation-only changes                                     |
| `test`     | Adding or correcting tests                                     |
| `refactor` | A code change that neither fixes a bug nor adds a feature      |
| `perf`     | A performance improvement                                      |
| `build`    | Build system or dependency changes                             |
| `ci`       | CI configuration and scripts                                   |
| `chore`    | Routine maintenance that doesn't touch `src`                   |
| `revert`   | Reverting a previous commit                                    |

Rules:

- The description is a short, imperative-mood summary ("add", not "added").
- Keep the subject line at 72 characters or fewer; don't end it with a period.
- Use the optional body to explain *what* and *why*, not *how*.
- Breaking changes **must** be flagged with a `!` after the type/scope
  (e.g. `feat!: drop Python 3.10 support`) and/or a `BREAKING CHANGE:` footer.
- A scope is encouraged when it clarifies the area touched, e.g.
  `feat(cli): ...`, `fix(config): ...`.

Examples:

```
feat(cli): support all CLI options as profile keys in prefs
fix(config): expire gather cache at UTC day boundary
docs: document profile keys in the README
```

## Before you open a PR

- Run the test suite: `python -m pytest`.
- Keep changes focused; prefer one logical change per commit.
