"""Web tools for the Issue Assistant agent: web_search and fetch_url.

The two tools are declared as ``Tool`` schemas and exposed through the
``TOOLS`` constant, which is the single source of truth rendered to
provider-native form by ``tools_to_openai`` before being passed to
``LLMClient.chat``.

Every tool error is *returned* as a result (``{"error": ...}``) rather than
raised, so the model can recover and answer from partial data. The only
exceptions that propagate are configuration errors (unknown search provider).
"""

import gzip
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import trafilatura
from ddgs import DDGS

from jobgitops.schema import ResearchConfig, ValidationError

logger = logging.getLogger(__name__)

# Browser-like User-Agent so sites don't reject the plain urllib fetch.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Allowed ports per the SSRF policy; anything else is rejected outright.
_ALLOWED_PORTS = (80, 443)

# Domains that are effectively JavaScript apps where plain HTML fetches come
# back empty/gated; they trigger the Jina Reader fallback unconditionally.
_JS_HEAVY_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "workable.com",
    "recruitee.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "bamboohr.com",
    "workday.com",
    "myworkdayjobs.com",
    "icims.com",
    "jobvite.com",
    "taleo.net",
)


def _error(message: str) -> dict[str, str]:
    """Return a tool-error result instead of raising, so the model can recover.

    Logged as a warning so production failures are observable even though the
    model continues from the returned error result.
    """
    logger.warning("Web tool returned an error result: %s", message)
    return {"error": message}


@dataclass(frozen=True)
class Tool:
    """A tool schema the agent can call.

    Attributes:
        name: The tool name the model invokes.
        description: Model-facing explanation of what the tool does.
        parameters: JSON Schema object (OpenAI-style: type, properties, required).
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    """A single web search result."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class PageContent:
    """Extracted readable content from a fetched page."""

    url: str
    title: str
    text: str
    source: str  # "direct" | "jina"


def tools_to_openai(tools: list[Tool]) -> list[dict[str, Any]]:
    """Render ``Tool`` schemas to the OpenAI-style form ``LLMClient.chat`` takes."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


web_search = Tool(
    name="web_search",
    description=(
        "Search the web for facts about a company or job role. Returns up to N "
        "results with title, URL, and snippet."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query."}},
        "required": ["query"],
    },
)

fetch_url = Tool(
    name="fetch_url",
    description=(
        "Fetch and read a URL's readable content (job posting, company website "
        "page, news article). Returns page title and extracted text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to fetch."}
        },
        "required": ["url"],
    },
)

TOOLS = [web_search, fetch_url]


class _FetchError(Exception):
    """Base class for fetch failures returned as tool results."""


class _RedirectCapExceededError(_FetchError):
    """Raised when the redirect chain exceeds ``max_redirects``."""


class _TotalTimeoutError(_FetchError):
    """Raised when the overall fetch exceeds ``total_timeout_seconds``."""


class _ContentTooLargeError(_FetchError):
    """Raised when the response body exceeds ``max_content_bytes``."""


class _HttpError(_FetchError):
    """Raised for HTTP/network failures; carries a model-facing message."""


class _CappedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler enforcing cap, total budget, and scheme/port policy.

    urllib follows redirects internally without a hop limit we can tune, so the
    cap and the total-time budget are enforced here, between hops. Redirect
    targets are re-checked against the scheme/port policy so a redirect cannot
    smuggle the request to a ``file://`` URL or a non-allowlisted port. With
    ``block_private_ips`` set, redirect hosts are also resolved and rejected
    when they point at private/loopback/link-local addresses, closing the SSRF
    hole where an external page redirects the fetch onto an internal service.
    """

    def __init__(
        self,
        max_redirects: int,
        deadline: float,
        block_private_ips: bool = False,
    ) -> None:
        super().__init__()
        self.max_redirects = max_redirects
        self.deadline = deadline
        self.block_private_ips = block_private_ips
        self.count = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self.count += 1
        if self.count > self.max_redirects:
            raise _RedirectCapExceededError(
                f"Too many redirects (max {self.max_redirects})"
            )
        if time.monotonic() > self.deadline:
            raise _TotalTimeoutError("Total fetch time budget exceeded")
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme not in ("http", "https"):
            raise _HttpError(f"Redirect to unsupported scheme '{parsed.scheme}'")
        if parsed.port is not None and parsed.port not in _ALLOWED_PORTS:
            raise _HttpError("Redirect to a non-allowlisted port")
        if self.block_private_ips and not _host_is_public(parsed.hostname or ""):
            raise _HttpError(
                "Redirect target resolves to a private, loopback, or "
                "link-local address (SSRF guard)."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_private_ip(ip_str: str) -> bool:
    """Return True for loopback/private/link-local/multicast/reserved addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_is_public(hostname: str) -> bool:
    """Resolve a hostname and confirm every address is public (DNS-rebinding guard).

    Fails closed: unresolvable hostnames return False so the fetch is blocked
    rather than sent to an address we could not verify.
    """
    host = hostname.strip().lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(not _is_private_ip(info[4][0]) for info in infos)


def _verify_public_peer(resp: Any) -> None:
    """Confirm the actually-connected peer is public; skip when not introspectable.

    ``_host_is_public`` resolves a hostname before the request opens, but urllib
    resolves it again when connecting, so a DNS-rebinding attacker could flip the
    answer in between. This checks the real socket peer after the connection is
    established. When the response shape hides the socket (or in tests where the
    opener is mocked), the check is skipped rather than crashing.
    """
    try:
        peer = resp.fp.raw._sock.getpeername()[0]
    except (AttributeError, OSError, IndexError, TypeError):
        return
    if not isinstance(peer, str):
        return
    if _is_private_ip(peer):
        raise _HttpError(
            "Blocked: connected to a private, loopback, or link-local "
            "address (SSRF guard)."
        )


def _is_js_heavy(hostname: str) -> bool:
    """Return True when the host is a known JavaScript-heavy job board."""
    host = hostname.lower().removeprefix("www.")
    return any(
        host == domain or host.endswith("." + domain) for domain in _JS_HEAVY_DOMAINS
    )


_TITLE_REGEX = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _extract_title(html: str) -> str:
    """Pull the document <title>; empty string when absent."""
    match = _TITLE_REGEX.search(html)
    return match.group(1).strip() if match else ""


def _extract_text(html: str) -> str:
    """Extract readable text from HTML via trafilatura; '' when nothing found."""
    try:
        text = trafilatura.extract(
            html,
            output_format="txt",
            include_comments=False,
            include_links=False,
            deduplicate=True,
        )
    except Exception:
        return ""
    return (text or "").strip()


def _parse_jina_body(body: str) -> tuple[str, str]:
    """Split a Jina Reader response into (title, markdown text)."""
    title = ""
    text = body
    if body.startswith("Title:"):
        first_newline = body.find("\n")
        title = (
            body[len("Title:") : first_newline].strip() if first_newline != -1 else ""
        )
    marker = "Markdown Content:"
    if marker in body:
        text = body.split(marker, 1)[1].strip()
    return title, text


def _decompress_gzip_limited(data: bytes, limit: int) -> bytes:
    """Decompress a gzip stream, aborting once the output exceeds ``limit`` bytes.

    Decompresses incrementally in small reads so a gzip "bomb" (tiny compressed
    payload that expands enormously) cannot exhaust memory before the size cap
    is enforced. Raises ``_ContentTooLargeError`` when the cap is exceeded.
    """
    chunks: list[bytes] = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(8192)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _ContentTooLargeError(f"Response body exceeds {limit} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


class WebClient:
    """Executes the agent's ``web_search`` and ``fetch_url`` tools.

    One instance is created per agent run, so in-memory memoization of
    identical calls and the per-run Jina call counter are scoped naturally.

    Attributes:
        research: The parsed ``research`` config section (settings).
    """

    def __init__(
        self,
        research: ResearchConfig,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.research = research
        # Injectable sleep so tests can observe the DDG politeness delay without
        # actually waiting.
        self._sleep = sleep
        # Memoize identical tool calls within one run (spec §9.4).
        self._memo: dict[tuple[str, str], Any] = {}
        self._jina_calls_used: int = 0

    def _memoized(self, name: str, argument: str, impl: Callable[[], Any]) -> Any:
        """Execute a tool call once per (tool, argument) within this run."""
        key = (name, argument)
        if key in self._memo:
            return self._memo[key]
        result = impl()
        self._memo[key] = result
        return result

    def web_search(self, query: str) -> list[SearchResult] | dict[str, str]:
        """Search the web using the configured provider.

        Returns a list of ``SearchResult`` on success or an error result
        ``{"error": ...}`` the model can recover from.

        Raises:
            ValidationError: For an unknown search provider (config error).
        """
        return self._memoized("web_search", query, lambda: self._search(query))

    def fetch_url(self, url: str) -> PageContent | dict[str, str]:
        """Fetch and extract the readable content of an http(s) URL.

        Returns a ``PageContent`` on success or an error result
        ``{"error": ...}``; never raises for a per-URL failure.
        """
        return self._memoized("fetch_url", url, lambda: self._fetch(url))

    # -- web_search -----------------------------------------------------------

    def _search(self, query: str) -> list[SearchResult] | dict[str, str]:
        provider = (self.research.search_provider or "").strip().lower()
        if provider == "duckduckgo":
            return self._search_duckduckgo(query)
        if provider == "tavily":
            return self._search_tavily(query)
        if provider == "brave":
            return self._search_brave(query)
        raise ValidationError(
            f"Unknown search provider '{self.research.search_provider}'. "
            "Allowed values: duckduckgo, tavily, brave."
        )

    def _search_duckduckgo(self, query: str) -> list[SearchResult] | dict[str, str]:
        if self.research.request_delay > 0:
            # Politeness delay between DuckDuckGo requests (unofficial client).
            self._sleep(self.research.request_delay)
        try:
            results = DDGS().text(
                query, max_results=self.research.max_results, region="wt-wt"
            )
        except Exception as e:
            return _error(f"DuckDuckGo search failed: {e}")
        return [
            SearchResult(
                title=str(result.get("title", "")),
                url=str(result.get("href", "")),
                snippet=str(result.get("body", "")),
            )
            for result in results
        ]

    def _search_tavily(self, query: str) -> list[SearchResult] | dict[str, str]:
        api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not api_key:
            return _error("TAVILY_API_KEY is not set")
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": self.research.max_results,
            "search_depth": "basic",
        }
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.research.timeout_seconds
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            return _error(f"Tavily search failed: {e}")
        return [
            SearchResult(
                title=str(result.get("title", "")),
                url=str(result.get("url", "")),
                snippet=str(result.get("content", "")),
            )
            for result in data.get("results", [])
        ]

    def _search_brave(self, query: str) -> list[SearchResult] | dict[str, str]:
        api_key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not api_key:
            return _error("BRAVE_API_KEY is not set")
        params = urllib.parse.urlencode(
            {"q": query, "count": self.research.max_results}
        )
        req = urllib.request.Request(
            f"https://api.search.brave.com/res/v1/web/search?{params}",
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.research.timeout_seconds
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            return _error(f"Brave search failed: {e}")
        return [
            SearchResult(
                title=str(result.get("title", "")),
                url=str(result.get("url", "")),
                snippet=str(result.get("description", "")),
            )
            for result in data.get("web", {}).get("results", [])
        ]

    # -- fetch_url ------------------------------------------------------------

    def _build_opener(
        self, max_redirects: int, total_timeout: float
    ) -> urllib.request.OpenerDirector:
        """Build an opener enforcing the redirect cap, budget, and SSRF policy."""
        deadline = time.monotonic() + total_timeout
        handler = _CappedRedirectHandler(
            max_redirects,
            deadline,
            block_private_ips=self.research.block_private_ips,
        )
        return urllib.request.build_opener(handler)

    def _open(
        self, url: str, timeout: float, total_timeout: float, max_redirects: int
    ) -> Any:
        """Open a URL, mapping HTTP/network/timeout failures to ``_HttpError``."""
        opener = self._build_opener(max_redirects, total_timeout)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"},
        )
        # Per-request timeout capped by the remaining total budget so the two
        # limits compose across redirect hops.
        per_request = min(timeout, total_timeout)
        try:
            return opener.open(req, timeout=per_request)
        except urllib.error.HTTPError as e:
            raise _HttpError(f"HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise _HttpError(f"Connection error: {e.reason}") from e
        except TimeoutError as e:
            raise _HttpError("Request timed out") from e

    def _read_body(self, resp: Any, max_bytes: int) -> bytes:
        """Read a response body under the size cap, decoding gzip when present."""
        raw = resp.read(max_bytes + 1)
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            if len(raw) > max_bytes:
                raise _ContentTooLargeError(f"Response body exceeds {max_bytes} bytes")
            try:
                raw = _decompress_gzip_limited(raw, max_bytes)
            except (gzip.BadGzipFile, OSError, EOFError) as e:
                raise _HttpError(f"Invalid gzip body: {e}") from e
        if len(raw) > max_bytes:
            raise _ContentTooLargeError(f"Response body exceeds {max_bytes} bytes")
        return raw

    def _http_get(
        self,
        url: str,
        timeout: float,
        total_timeout: float,
        max_redirects: int,
        max_bytes: int,
    ) -> tuple[str, bytes]:
        """GET a URL with the SSRF guards and caps enforced.

        Returns ``(final_url, body)`` where ``body`` is the decoded
        (gzip-expanded) payload.

        Raises:
            _FetchError subclasses for policy/cap violations and HTTP failures.
        """
        resp = self._open(url, timeout, total_timeout, max_redirects)
        with resp:
            if self.research.block_private_ips:
                _verify_public_peer(resp)
            raw = self._read_body(resp, max_bytes)
            return resp.geturl(), raw

    def _fetch(self, url: str) -> PageContent | dict[str, str]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return _error(
                f"Unsupported URL scheme '{parsed.scheme}'. Only http(s) is allowed."
            )
        if parsed.port is not None and parsed.port not in _ALLOWED_PORTS:
            return _error(
                f"Port {parsed.port} is not allowed. Only ports "
                f"{', '.join(str(p) for p in _ALLOWED_PORTS)} are allowed."
            )
        hostname = parsed.hostname or ""
        if self.research.block_private_ips and not _host_is_public(hostname):
            return _error(
                "Blocked: hostname resolves to a private, loopback, or "
                "link-local address (SSRF guard)."
            )

        if self.research.use_jina_reader and _is_js_heavy(hostname):
            # JS-heavy boards need the rendering Jina provides; skip the wasted
            # plain fetch and go straight to Jina, falling back to plain on error.
            jina_result = self._jina_try(url)
            if jina_result is not None:
                return jina_result
            return self._fetch_direct(url)

        direct_result = self._fetch_direct(url)
        if isinstance(direct_result, PageContent) and not self._should_jina(
            direct_result
        ):
            return direct_result
        jina_result = self._jina_try(url)
        if jina_result is not None:
            return jina_result
        return direct_result

    def _jina_try(self, url: str) -> PageContent | None:
        """Attempt the Jina Reader fallback when the budget allows.

        Returns None (after logging) when Jina is disabled, out of budget, or
        fails, so the caller falls back to the plain fetch result.
        """
        if not self.research.use_jina_reader:
            return None
        if self._jina_calls_used >= self.research.max_jina_calls:
            logger.warning(
                "Jina Reader call budget exhausted (%d); skipping fallback for %s",
                self.research.max_jina_calls,
                url,
            )
            return None
        self._jina_calls_used += 1
        jina_result = self._fetch_jina(url)
        if jina_result is not None:
            return jina_result
        logger.warning("Jina Reader fallback failed for %s; using the plain fetch", url)
        return None

    def _fetch_direct(self, url: str) -> PageContent | dict[str, str]:
        """Plain-fetch path; returns a PageContent or an error result."""
        try:
            final_url, raw = self._http_get(
                url,
                timeout=float(self.research.timeout_seconds),
                total_timeout=float(self.research.total_timeout_seconds),
                max_redirects=self.research.max_redirects,
                max_bytes=self.research.max_content_bytes,
            )
        except _FetchError as e:
            return _error(str(e))
        html = raw.decode("utf-8", errors="replace")
        text = _extract_text(html)
        if text:
            return PageContent(
                url=final_url,
                title=_extract_title(html),
                text=text,
                source="direct",
            )
        return _error("No readable content extracted from the page")

    def _should_jina(self, direct_result: PageContent | dict[str, str]) -> bool:
        """Jina fallback triggers when the direct fetch was empty or failed."""
        if not self.research.use_jina_reader:
            return False
        return isinstance(direct_result, dict)

    def _fetch_jina(self, url: str) -> PageContent | None:
        """Re-fetch via the Jina Reader (r.jina.ai) which renders JS pages.

        Returns None on failure so the caller falls back to the plain result.
        """
        jina_url = "https://r.jina.ai/" + url
        try:
            _, raw = self._http_get(
                jina_url,
                timeout=float(self.research.timeout_seconds),
                total_timeout=float(self.research.total_timeout_seconds),
                max_redirects=self.research.max_redirects,
                max_bytes=self.research.max_content_bytes,
            )
        except _FetchError:
            return None
        body = raw.decode("utf-8", errors="replace")
        title, text = _parse_jina_body(body)
        if not text:
            return None
        return PageContent(url=url, title=title, text=text, source="jina")
