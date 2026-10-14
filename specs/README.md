# Herald design specifications

These documents describe the implemented data contracts and flows. Proposed
changes are labeled separately; an issue or PR is not evidence that a change
has shipped. Update the relevant spec when changing a payload, transformation,
cache boundary, or publication outcome.

- [Initial drafting contract](initial-drafting.md): exactly what is sent to the
  model, what is omitted, and how the request and response are handled.
- [End-to-end data flows](data-flows.md): gathering, local artifacts, caching,
  review, revision, refresh, and verified Jira publication.

The initial baseline is `main` at `4a65b39` (publication fix #7 merged).
Draft validation in [PR #8](https://github.com/celebdor/herald/pull/8) is described
as a proposed contract change until merged. The external gathering script and
reporting rules can vary with the configured status-analysis checkout; these
specs document Herald's interface to them rather than claiming a fixed upstream
implementation.
