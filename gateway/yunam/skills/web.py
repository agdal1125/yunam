"""Web-browsing skill — wraps `WebTools` with scopes + schemas + prompt fragment.

Keyless by default (Jina Reader / Jina Search). An optional `JINA_API_KEY`
raises the rate limit without changing behavior. Falls back to DuckDuckGo
HTML on Jina failure.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.web import WebTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "web"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Web browsing

You can search the open web and fetch individual URLs as readable text.

Reach for these tools when the question needs information that's fresher than
your training data, when jaekeun quotes a URL, or when you'd otherwise have to
guess at current facts (news, prices, release dates, documentation).

- `web_search` — keyword query, returns a short list of results with titles,
  URLs, and snippets. Use this first when you don't already have a URL.
- `web_fetch` — pull one URL's main content as markdown. Use this after
  `web_search` to read a specific result, or directly when jaekeun gives a URL.

Keep it tight: one search + one or two fetches is usually enough. Don't fetch
the same page twice in a turn. Prefer authoritative sources (official docs,
first-party sites) over aggregators when both show up.

The tools only speak HTTP/HTTPS. Local/private addresses are refused. Responses
are capped at ~500 KB — if a page is truncated, the tail of the content will
say so. If a fetch fails, say so briefly rather than fabricating.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "web_search": {
        "name": "web_search",
        "description": (
            "Search the open web for a query. Returns a short list of results "
            "(title, URL, snippet) as plain text. Use this when you need current "
            "information or don't yet have a specific URL. Follow up with "
            "`web_fetch` on the most relevant result to read the full page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query. Plain keywords work best.",
                },
                "num": {
                    "type": "integer",
                    "description": "Maximum results to return (1–10). Defaults to 5.",
                },
            },
            "required": ["query"],
        },
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": (
            "Fetch a single URL and return its main content as readable text "
            "(markdown where possible). Use after `web_search` to read a result, "
            "or directly when the user provides a URL. Only http/https is allowed; "
            "local/private addresses are refused. Responses are capped at ~500 KB "
            "and are truncated with a trailing marker if larger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http(s) URL to fetch.",
                },
            },
            "required": ["url"],
        },
    },
}


def build_web_skill(tools: WebTools) -> Skill:
    """Wrap a resolved `WebTools` instance as a Skill."""

    async def _search(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.web_search(**inputs)

    async def _fetch(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.web_fetch(**inputs)

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("web_search", Scope.WEB_SEARCH, _SCHEMAS["web_search"], _search),
        ToolSpec("web_fetch", Scope.WEB_FETCH, _SCHEMAS["web_fetch"], _fetch),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
