"""Web search + fetch primitives — Jina first, minimal fallback.

- `r.jina.ai/<url>` (fetch) — keyless, rate-limited, returns model-ready markdown.
- `s.jina.ai/?q=<query>` (search) — requires `JINA_API_KEY` (401 without one).
  Without a key, search goes straight to the DuckDuckGo HTML fallback and
  skips the guaranteed-401 Jina round-trip.

Fallback paths:
  - fetch: direct `httpx.get` with a browser UA
  - search: DuckDuckGo HTML endpoint, crude result parse

Keeping this layer narrow on purpose — the skill layer
(`yunam/skills/web.py`) owns schemas, prompt guidance, and scope assignment.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Final
from urllib.parse import quote_plus, unquote_plus, urlparse

import httpx

from ..usage import UsageRecorder

logger = logging.getLogger(__name__)


class WebError(Exception):
    """Raised by web primitives for anything the tool should surface to the model."""


# 80 KB ≈ 20k Claude tokens per fetch — big enough for most article/doc bodies,
# small enough that a single fetch can't blow up the next turn's input tokens.
# Earlier 500 KB cap caused ~125k-token turns when a verbose page came back.
MAX_BYTES: Final = 80_000
DEFAULT_TIMEOUT_S: Final = 15.0
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_PRIVATE_HOST_PREFIXES = ("10.", "192.168.", "169.254.", "172.")
_PRIVATE_HOST_NAMES = {"localhost", "0.0.0.0", "127.0.0.1", "::1"}


def _validate_url(url: str) -> str:
    if not url:
        raise WebError("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise WebError(f"unsupported scheme: {parsed.scheme!r} (only http/https allowed)")
    if not parsed.netloc:
        raise WebError(f"invalid url: {url!r}")
    host = (parsed.hostname or "").lower()
    # Minimal SSRF guard — not DNS-resolving, just the obvious cases. Good
    # enough for a Tokyo VPS that doesn't have other services on private IPs.
    if host in _PRIVATE_HOST_NAMES or host.startswith(_PRIVATE_HOST_PREFIXES):
        raise WebError(f"refusing to fetch local/private address: {host!r}")
    return url


def _truncate(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    # Decode back a conservatively-clipped prefix; errors='ignore' handles the
    # case where we cut inside a multi-byte codepoint.
    clipped = data[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n\n…(truncated)"


class WebTools:
    """Async web search + fetch. One instance per process; safe to share across turns."""

    def __init__(
        self,
        *,
        jina_api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        usage_recorder: UsageRecorder | None = None,
    ):
        self._jina_api_key = jina_api_key
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._usage = usage_recorder

    def _record(
        self,
        *,
        provider: str,
        endpoint: str,
        t0: float,
        status: str,
    ) -> None:
        if self._usage is None:
            return
        self._usage.record_rest(
            provider=provider,
            endpoint=endpoint,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            status=status,
        )

    def _jina_headers(self) -> dict[str, str]:
        headers = {"Accept": "text/plain"}
        if self._jina_api_key:
            headers["Authorization"] = f"Bearer {self._jina_api_key}"
        return headers

    async def web_fetch(self, url: str) -> str:
        url = _validate_url(url)
        try:
            return await self._jina_fetch(url)
        except Exception as e:
            logger.info("jina fetch failed url=%s err=%r; falling back to direct", url, e)
        return await self._direct_fetch(url)

    async def web_search(self, query: str, num: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            raise WebError("query is required")
        if num < 1 or num > 10:
            num = 5
        # Jina Search requires an API key (Reader does not). Skip it entirely
        # when unset to save the round-trip on the guaranteed 401.
        if self._jina_api_key:
            try:
                return await self._jina_search(query)
            except Exception as e:
                logger.info("jina search failed query=%r err=%r; falling back to DDG", query, e)
        return await self._ddg_search(query, num)

    async def _jina_fetch(self, url: str) -> str:
        endpoint = f"https://r.jina.ai/{url}"
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s, follow_redirects=True) as client:
                r = await client.get(endpoint, headers=self._jina_headers())
                r.raise_for_status()
                return _truncate(r.text, self._max_bytes)
        except Exception:
            status = "error"
            raise
        finally:
            self._record(provider="jina", endpoint="reader", t0=t0, status=status)

    async def _direct_fetch(self, url: str) -> str:
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": _UA},
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                content_type = r.headers.get("content-type", "").lower()
                if not any(t in content_type for t in ("text/", "json", "xml", "html")):
                    raise WebError(f"non-text content-type: {content_type!r}")
                return _truncate(r.text, self._max_bytes)
        except Exception:
            status = "error"
            raise
        finally:
            self._record(provider="direct", endpoint="fetch", t0=t0, status=status)

    async def _jina_search(self, query: str) -> str:
        endpoint = f"https://s.jina.ai/?q={quote_plus(query)}"
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s, follow_redirects=True) as client:
                r = await client.get(endpoint, headers=self._jina_headers())
                r.raise_for_status()
                return _truncate(r.text, self._max_bytes)
        except Exception:
            status = "error"
            raise
        finally:
            self._record(provider="jina", endpoint="search", t0=t0, status=status)

    async def _ddg_search(self, query: str, num: int) -> str:
        endpoint = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": _UA},
            ) as client:
                r = await client.get(endpoint)
                r.raise_for_status()
                html = r.text
        except Exception:
            status = "error"
            self._record(provider="duckduckgo", endpoint="html", t0=t0, status=status)
            raise
        self._record(provider="duckduckgo", endpoint="html", t0=t0, status=status)

        link_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.+?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.+?)</a>',
            re.DOTALL,
        )
        matches = list(link_pattern.finditer(html))
        if not matches:
            raise WebError("ddg returned no parseable results (markup may have changed)")
        snippets = [m.group("snippet") for m in snippet_pattern.finditer(html)]

        lines = [f"DuckDuckGo results for: {query}", ""]
        for i, m in enumerate(matches[:num]):
            raw_url = m.group("url")
            title = re.sub(r"<[^>]+>", "", m.group("title")).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            # DDG wraps outbound links via //duckduckgo.com/l/?uddg=<encoded>
            if raw_url.startswith("//duckduckgo.com/l/?") or raw_url.startswith(
                "https://duckduckgo.com/l/?"
            ):
                uddg = re.search(r"[?&]uddg=([^&]+)", raw_url)
                if uddg:
                    raw_url = unquote_plus(uddg.group(1))
            lines.append(f"{i + 1}. {title}")
            lines.append(f"   {raw_url}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
