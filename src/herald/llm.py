"""LLM client creation, cost tracking, drafting, and system prompt building."""

import inspect
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

import anthropic

from .config import DEFAULT_MAX_TOKENS, UserPrefs, find_skills_dir


_TEMPORAL_EVIDENCE_RULES = (
    "Temporal evidence rules:\n"
    "- Treat only events in `events_in_window` with an event-specific `occurred_at` "
    "as evidence that an accomplishment happened during this reporting window.\n"
    "- `carry_forward_context` describes current or historical state. It may support "
    "a continuing risk or blocker, but does not establish when an accomplishment occurred.\n"
    "- A pull request's current state or `merged_at` in carry-forward context does not "
    "make its merge a new accomplishment this week. A dated in-window review or commit "
    "may be reported as that specific activity.\n"
    "- A recent comment is evidence that the comment was made, not that older work "
    "described in it happened during this window.\n"
    "- Treat `undated_activity` and unknown reporting-window or gather-freshness metadata "
    "as uncertainty. Do not turn it into a new accomplishment or infer a date.\n"
    "- It is valid to report no new milestone this week. Describe recurring blockers and "
    "risks as continuing conditions.\n"
)

_RYG_MARKDOWN_FORMAT_RULES = (
    "Produce only the `ryg_field_markdown` output format for Jira Cloud REST API v3. "
    "No preamble or explanation; output starts with `* Color Status:`. "
    "Use standard Markdown bullets: top-level bullets are `* ` and nested bullets "
    "are `  * ` (two spaces, asterisk, space). Never use Jira wiki-style `**` "
    "sub-bullets. Render every PR reference as a Markdown link labeled "
    "`owner/repo#number` and linked to the PR URL in the evidence, using the "
    "provided `repository` and `url` fields. Never output a bare `#number` or "
    "shorthand `repo#number`, or put a raw PR URL in parentheses. The URL must "
    "appear only as the Markdown link target. Do not guess a repository or URL "
    "when the evidence does not provide one."
)


LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_pricing_cache: Optional[dict] = None


def create_client():
    """Create Anthropic client — Vertex AI if configured, else direct API."""
    vertex_project = (
        os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    vertex_region = os.environ.get("CLOUD_ML_REGION", "us-east5")
    if vertex_project:
        return anthropic.AsyncAnthropicVertex(
            project_id=vertex_project, region=vertex_region
        )
    return anthropic.AsyncAnthropic()


def fetch_model_pricing(model_id: str, is_vertex: bool = False) -> Optional[dict]:
    """Fetch per-token pricing from the LiteLLM database with module-level cache."""
    global _pricing_cache
    if _pricing_cache is None:
        try:
            resp = urllib.request.urlopen(LITELLM_PRICING_URL, timeout=5)
            _pricing_cache = json.loads(resp.read().decode())
        except Exception as e:
            print(f"Warning: Failed to fetch model pricing: {e}", file=sys.stderr)
            return None

    lookup = model_id
    if is_vertex:
        lookup = f"vertex_ai/{model_id}"

    raw = _pricing_cache.get(lookup)
    if raw is None:
        candidates = [k for k in _pricing_cache if model_id in k]
        if candidates:
            raw = _pricing_cache[candidates[0]]
    if raw is None:
        print(f"Warning: Model '{model_id}' not found in pricing database", file=sys.stderr)
        return None

    return {
        "input": raw.get("input_cost_per_token", 0) * 1_000_000,
        "output": raw.get("output_cost_per_token", 0) * 1_000_000,
        "cache_write": raw.get("cache_creation_input_token_cost", 0) * 1_000_000,
        "cache_read": raw.get("cache_read_input_token_cost", 0) * 1_000_000,
    }


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def add(self, response) -> None:
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cache_write_tokens += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(response.usage, "cache_read_input_tokens", 0) or 0
        self.calls += 1

    def cost(self, pricing: dict) -> float:
        return (
            self.input_tokens * pricing["input"] / 1_000_000
            + self.output_tokens * pricing["output"] / 1_000_000
            + self.cache_write_tokens * pricing.get("cache_write", 0) / 1_000_000
            + self.cache_read_tokens * pricing.get("cache_read", 0) / 1_000_000
        )


# Multiplier applied to max_tokens when a truncated response is retried.
TRUNCATION_RETRY_FACTOR = 2


class TruncatedResponseError(RuntimeError):
    """The response hit max_tokens before a complete draft was produced."""


def _first_text(response, max_tokens: Optional[int] = None) -> str:
    """Return the text of the first TextBlock, skipping any ThinkingBlocks.

    A gateway model that emits reasoning tokens shares max_tokens with the
    answer, so ``stop_reason == "max_tokens"`` usually means the thinking trace
    consumed the whole budget. Surface that as a truncation instead of returning
    partial thinking text that will fail draft validation. A capped response is
    retried even when it contains a TextBlock, because the visible draft may be
    cut off mid-sentence.
    """
    if getattr(response, "stop_reason", None) == "max_tokens":
        limit = f" at max_tokens={max_tokens}" if max_tokens is not None else ""
        raise TruncatedResponseError(
            f"Response was truncated{limit} before a complete draft was produced"
        )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    for block in response.content:
        if hasattr(block, "thinking"):
            return block.thinking
    raise ValueError(f"No usable content in response: {response.content}")


async def call_with_truncation_retry(call, max_tokens: int, on_retry=None) -> str:
    """Call ``call(max_tokens)``, retrying once with a doubled cap on truncation.

    ``call`` receives the effective output budget. ``on_retry`` is invoked with
    the new budget before the retry and may be sync or async. A second
    truncation propagates to the caller.
    """
    try:
        return await call(max_tokens)
    except TruncatedResponseError:
        retry_max_tokens = max_tokens * TRUNCATION_RETRY_FACTOR
        if on_retry is not None:
            result = on_retry(retry_max_tokens)
            if inspect.isawaitable(result):
                await result
        return await call(retry_max_tokens)


async def draft_ryg(
    client, model: str, system_prompt: str, issue_summary: str, usage: TokenUsage,
    *, max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Draft R/Y/G status for one issue. One independent API call."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": issue_summary}],
    )
    usage.add(response)
    return _first_text(response, max_tokens)


async def request_changes_api(
    client, model: str, draft: str, issue_summary: str, instruction: str, usage: TokenUsage,
    *, max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Revise a draft via a single Anthropic API call (no tool access)."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=(
            "You are revising a Jira R/Y/G status update. Output ONLY the revised "
            "status update in the exact same format.\n\n"
            + _TEMPORAL_EVIDENCE_RULES
            + _RYG_MARKDOWN_FORMAT_RULES
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Issue summary:\n{issue_summary}\n\n"
                f"Current draft:\n{draft}\n\n"
                f"Requested change: {instruction}"
            ),
        }],
    )
    usage.add(response)
    return _first_text(response, max_tokens)


def build_system_prompt(prefs: UserPrefs) -> str:
    """Build system prompt from the canonical skill documents.

    activity-analysis.md and formatting.md are the single source of truth for
    R/Y/G rules. Including them verbatim here means the app stays in sync with
    the Claude Code skill automatically. The prompt is identical across all
    parallel API calls, so Anthropic's prompt caching applies.
    """
    skills_dir = find_skills_dir(prefs)
    parts = [
        "You are a Jira status analyst producing weekly R/Y/G status updates.",
        "Apply the analysis methodology and output format defined below.\n",
        _TEMPORAL_EVIDENCE_RULES,
    ]
    for filename in ("activity-analysis.md", "formatting.md"):
        path = skills_dir / filename
        try:
            parts.append(f"## {filename}\n\n{path.read_text()}\n")
        except FileNotFoundError:
            pass

    if prefs.writing_rules:
        rules = "\n".join(f"- {rule}" for rule in prefs.writing_rules)
        parts.append(f"## Additional writing rules (user preferences)\n\n{rules}\n")

    parts.append(_RYG_MARKDOWN_FORMAT_RULES)
    return "\n".join(parts)
