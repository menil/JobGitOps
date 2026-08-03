"""Unit tests for the Issue Assistant web tools (spec 5.4)."""

import gzip
import json
import os
import urllib.error
import urllib.request
from unittest import mock

import pytest

import jobgitops.web as web
from jobgitops.schema import ResearchConfig, ValidationError
from jobgitops.web import (
    TOOLS,
    PageContent,
    SearchResult,
    WebClient,
    tools_to_openai,
)


def make_research(**overrides) -> ResearchConfig:
    """Build a ResearchConfig with test-friendly defaults."""
    defaults = {
        "search_provider": "duckduckgo",
        "max_results": 5,
        "max_iterations": 6,
        "max_context_comments": 10,
        "timeout_seconds": 15,
        "total_timeout_seconds": 30,
        "max_redirects": 5,
        "max_content_bytes": 262144,
        "request_delay": 0.0,
        "use_jina_reader": True,
        "max_jina_calls": 5,
        "block_private_ips": True,
    }
    defaults.update(overrides)
    return ResearchConfig(**defaults)


def make_client(research: ResearchConfig | None = None, **overrides) -> WebClient:
    """Build a WebClient with an injected no-op sleep for deterministic tests."""
    client = WebClient(research or make_research(**overrides), sleep=mock.MagicMock())
    return client


def mock_urlopen_response(body: bytes = b"{}") -> mock.MagicMock:
    """Create a mock urllib response compatible with context-manager use."""
    resp = mock.MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def make_fetch_opener(
    body: bytes = b"<html></html>",
    headers: dict[str, str] | None = None,
    url: str = "https://example.com/page",
) -> mock.MagicMock:
    """Create a mock opener whose open() returns a context-managed response."""
    opener = mock.MagicMock()
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.headers = headers or {}
    resp.geturl.return_value = url
    resp.__enter__.return_value = resp
    opener.open.return_value = resp
    return opener


# --- Tool schemas -----------------------------------------------------------


def test_tools_schema() -> None:
    """TOOLS exposes exactly the two spec'd tools with their JSON schemas."""
    names = {tool.name for tool in TOOLS}
    assert names == {"web_search", "fetch_url"}
    for tool in TOOLS:
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert "required" in tool.parameters


def test_tools_to_openai() -> None:
    """Tool dataclasses render to the OpenAI-style form LLMClient.chat takes."""
    tools = [
        web.Tool(
            name="web_search",
            description="Search the web.",
            parameters={"type": "object", "properties": {}},
        )
    ]
    rendered = tools_to_openai(tools)
    assert rendered[0]["type"] == "function"
    assert rendered[0]["function"]["name"] == "web_search"
    assert rendered[0]["function"]["description"] == "Search the web."
    assert rendered[0]["function"]["parameters"]["type"] == "object"


# --- web_search: provider selection and request shapes -----------------------


def test_web_search_duckduckgo() -> None:
    """DDG provider maps results and applies the politeness delay."""
    mock_ddgs = mock.MagicMock()
    mock_ddgs.return_value.text.return_value = [
        {"title": "Acme Careers", "href": "https://acme.com/careers", "body": "Join us"}
    ]
    client = make_client(request_delay=1.5)

    with mock.patch.object(web, "DDGS", mock_ddgs):
        result = client.web_search("Acme company")

    assert result == [
        SearchResult(
            title="Acme Careers",
            url="https://acme.com/careers",
            snippet="Join us",
        )
    ]
    mock_ddgs.return_value.text.assert_called_once_with(
        "Acme company", max_results=5, region="wt-wt"
    )
    client._sleep.assert_called_once_with(1.5)


def test_web_search_duckduckgo_no_delay_when_zero() -> None:
    """A zero request_delay skips the politeness sleep."""
    mock_ddgs = mock.MagicMock()
    mock_ddgs.return_value.text.return_value = []
    client = make_client(request_delay=0.0)
    with mock.patch.object(web, "DDGS", mock_ddgs):
        client.web_search("q")
    client._sleep.assert_not_called()


def test_web_search_tavily_request_shape(monkeypatch) -> None:
    """Tavily provider POSTs the right payload and maps results."""
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    resp_body = {
        "results": [{"title": "T1", "url": "https://x.com", "content": "Snippet"}]
    }
    mock_urlopen = mock.MagicMock()
    mock_urlopen.return_value = mock_urlopen_response(
        json.dumps(resp_body).encode("utf-8")
    )
    client = make_client(search_provider="tavily", max_results=3)

    with mock.patch("jobgitops.web.urllib.request.urlopen", mock_urlopen):
        result = client.web_search("tavily query")

    assert result == [SearchResult(title="T1", url="https://x.com", snippet="Snippet")]
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.tavily.com/search"
    assert req.method == "POST"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload == {
        "api_key": "tavily-key",
        "query": "tavily query",
        "max_results": 3,
        "search_depth": "basic",
    }


def test_web_search_brave_request_shape(monkeypatch) -> None:
    """Brave provider GETs the right URL with the subscription token header."""
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")
    resp_body = {
        "web": {
            "results": [{"title": "T1", "url": "https://x.com", "description": "S"}]
        }
    }
    mock_urlopen = mock.MagicMock()
    mock_urlopen.return_value = mock_urlopen_response(
        json.dumps(resp_body).encode("utf-8")
    )
    client = make_client(search_provider="brave", max_results=2)

    with mock.patch("jobgitops.web.urllib.request.urlopen", mock_urlopen):
        result = client.web_search("brave query")

    assert result == [SearchResult(title="T1", url="https://x.com", snippet="S")]
    req = mock_urlopen.call_args[0][0]
    assert req.method == "GET"
    assert (
        req.full_url
        == "https://api.search.brave.com/res/v1/web/search?q=brave+query&count=2"
    )
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers["x-subscription-token"] == "brave-key"


def test_web_search_tavily_missing_key(monkeypatch) -> None:
    """Tavily without an API key returns an error result, not a raise."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    client = make_client(search_provider="tavily")
    result = client.web_search("q")
    assert "TAVILY_API_KEY is not set" in result["error"]


def test_web_search_tavily_blank_key(monkeypatch) -> None:
    """A whitespace-only Tavily key is rejected as missing."""
    monkeypatch.setenv("TAVILY_API_KEY", "   ")
    client = make_client(search_provider="tavily")
    result = client.web_search("q")
    assert "TAVILY_API_KEY is not set" in result["error"]


def test_web_search_brave_blank_key(monkeypatch) -> None:
    """A whitespace-only Brave key is rejected as missing."""
    monkeypatch.setenv("BRAVE_API_KEY", "   ")
    client = make_client(search_provider="brave")
    result = client.web_search("q")
    assert "BRAVE_API_KEY is not set" in result["error"]


def test_web_search_unknown_provider() -> None:
    """An unknown search provider is a config error and raises ValidationError."""
    client = make_client(search_provider="google")
    with pytest.raises(ValidationError, match="Unknown search provider"):
        client.web_search("q")


def test_web_search_network_error_returned(monkeypatch) -> None:
    """Network failures are returned as tool results, never raised."""
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    mock_urlopen = mock.MagicMock()
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.tavily.com", code=500, msg="Server Error", hdrs=None, fp=None
    )
    client = make_client(search_provider="tavily")
    with mock.patch("jobgitops.web.urllib.request.urlopen", mock_urlopen):
        result = client.web_search("q")
    assert isinstance(result, dict)
    assert "Tavily search failed" in result["error"]
    assert "500" in result["error"]
    mock_urlopen.assert_called_once()


def test_web_search_memoization() -> None:
    """Identical searches within one run execute exactly once."""
    mock_ddgs = mock.MagicMock()
    mock_ddgs.return_value.text.return_value = [
        {"title": "T", "href": "https://x.com", "body": "S"}
    ]
    client = make_client(request_delay=1.0)
    with mock.patch.object(web, "DDGS", mock_ddgs):
        first = client.web_search("same query")
        second = client.web_search("same query")
    assert first == second
    assert mock_ddgs.return_value.text.call_count == 1
    assert client._sleep.call_count == 1


# --- fetch_url: guards ------------------------------------------------------


def test_fetch_url_scheme_guard() -> None:
    """Non-http(s) schemes are rejected outright."""
    client = make_client()
    result = client.fetch_url("file:///etc/passwd")
    assert "scheme" in result["error"]
    result = client.fetch_url("data://x")
    assert "scheme" in result["error"]


def test_fetch_url_port_allowlist() -> None:
    """Ports outside 80/443 are rejected."""
    client = make_client()
    result = client.fetch_url("https://example.com:8080/page")
    assert "port" in result["error"].lower()


def test_fetch_url_private_ip_blocked() -> None:
    """Loopback and RFC1918 addresses are blocked by the SSRF guard."""
    client = make_client()
    assert "Blocked" in client.fetch_url("http://127.0.0.1/x")["error"]
    assert "Blocked" in client.fetch_url("http://192.168.1.10/x")["error"]
    assert "Blocked" in client.fetch_url("http://localhost/x")["error"]


def test_fetch_url_private_ip_blocking_disabled() -> None:
    """With block_private_ips off, private addresses are fetched."""
    opener = make_fetch_opener(
        body=b"<html><title>Local</title></html>",
        headers={"Content-Encoding": "identity"},
    )
    client = make_client(block_private_ips=False)
    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_extract_text", return_value="Local text"),
    ):
        result = client.fetch_url("http://127.0.0.1/x")
    assert isinstance(result, PageContent)
    assert result.source == "direct"


def test_host_is_public() -> None:
    """Private/loopback addresses resolve to False; public addresses to True."""
    with mock.patch.object(web.socket, "getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (10, 1, 6, "", ("2606:2800:220:1::", 0)),
        ]
        assert web._host_is_public("example.com") is True

        mock_resolve.return_value = [(2, 1, 6, "", ("192.168.1.5", 0))]
        assert web._host_is_public("internal.example.com") is False

        mock_resolve.return_value = [(2, 1, 6, "", ("127.0.0.1", 0))]
        assert web._host_is_public("myhost.local") is False

        mock_resolve.side_effect = web.socket.gaierror("no such host")
        assert web._host_is_public("nope.invalid") is False

    assert web._host_is_public("localhost") is False


# --- fetch_url: fetch, caps, and decoding ------------------------------------


def test_fetch_url_success() -> None:
    """A successful direct fetch returns PageContent with source 'direct'."""
    html = "<html><title>Acme Careers</title><body>Join us</body></html>"
    opener = make_fetch_opener(
        body=html.encode("utf-8"), url="https://example.com/jobs"
    )
    client = make_client()

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value="Join us"),
    ):
        result = client.fetch_url("https://example.com/jobs")

    assert result == PageContent(
        url="https://example.com/jobs",
        title="Acme Careers",
        text="Join us",
        source="direct",
    )


def test_fetch_url_size_cap() -> None:
    """Bodies larger than max_content_bytes return an error result."""
    opener = make_fetch_opener(body=b"x" * 100)
    client = make_client(max_content_bytes=10, use_jina_reader=False)

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
    ):
        result = client.fetch_url("https://example.com/big")

    assert isinstance(result, dict)
    assert "exceeds" in result["error"]


def test_fetch_url_gzip_decode() -> None:
    """Gzip-encoded bodies are decoded before extraction."""
    html = "<html><title>Gz</title><p>Compressed content</p></html>"
    body = gzip.compress(html.encode("utf-8"))
    opener = make_fetch_opener(body=body, headers={"Content-Encoding": "gzip"})
    client = make_client()

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value="Compressed") as extract,
    ):
        result = client.fetch_url("https://example.com/page")

    assert result == PageContent(
        url="https://example.com/page",
        title="Gz",
        text="Compressed",
        source="direct",
    )
    extract.assert_called_once_with(html)


def test_fetch_url_gzip_bomb_rejected() -> None:
    """Gzip bodies whose expanded size exceeds the cap are rejected mid-read."""
    bomb = gzip.compress(b"A" * 100_000)
    opener = make_fetch_opener(
        body=bomb,
        headers={"Content-Encoding": "gzip"},
        url="https://example.com/bomb",
    )
    client = make_client(max_content_bytes=1_000, use_jina_reader=False)

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
    ):
        result = client.fetch_url("https://example.com/bomb")

    assert isinstance(result, dict)
    assert "exceeds" in result["error"]


def test_fetch_url_http_error_returned() -> None:
    """HTTP failures are returned as tool results, never raised."""
    opener = mock.MagicMock()
    opener.open.side_effect = urllib.error.HTTPError(
        url="https://example.com/missing",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    client = make_client(use_jina_reader=False)

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
    ):
        result = client.fetch_url("https://example.com/missing")

    assert isinstance(result, dict)
    assert "404" in result["error"]


def test_redirect_handler_cap() -> None:
    """The redirect handler raises once the redirect cap is exceeded."""
    handler = web._CappedRedirectHandler(max_redirects=2, deadline=float("inf"))
    req = urllib.request.Request("https://example.com/1")
    for _ in range(2):
        assert (
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://example.com/2"
            )
            is not None
        )
    with pytest.raises(web._RedirectCapExceededError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/2")


def test_redirect_handler_scheme_and_port_guards() -> None:
    """Redirects cannot smuggle to non-http(s) schemes or non-allowlisted ports."""
    handler = web._CappedRedirectHandler(max_redirects=5, deadline=float("inf"))
    req = urllib.request.Request("https://example.com/1")
    with pytest.raises(web._HttpError):
        handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
    with pytest.raises(web._HttpError):
        handler.redirect_request(
            req, None, 302, "Found", {}, "https://example.com:8080/x"
        )


def test_redirect_handler_total_timeout() -> None:
    """Redirects past the total-time deadline are cut off."""
    with mock.patch.object(web.time, "monotonic", return_value=100.0):
        handler = web._CappedRedirectHandler(max_redirects=5, deadline=99.0)
        req = urllib.request.Request("https://example.com/1")
        with pytest.raises(web._TotalTimeoutError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://example.com/2"
            )


def test_redirect_handler_private_ip_blocked() -> None:
    """Redirects to private/loopback hosts are rejected by the SSRF guard."""
    handler = web._CappedRedirectHandler(
        max_redirects=5, deadline=float("inf"), block_private_ips=True
    )
    req = urllib.request.Request("https://example.com/1")
    with (
        mock.patch.object(web, "_host_is_public", return_value=False),
        pytest.raises(web._HttpError, match="SSRF"),
    ):
        handler.redirect_request(
            req, None, 302, "Found", {}, "https://169.254.169.254/latest/meta-data/"
        )


def test_redirect_handler_public_redirect_follows() -> None:
    """Redirects to public hosts pass the SSRF guard and are followed."""
    handler = web._CappedRedirectHandler(
        max_redirects=5, deadline=float("inf"), block_private_ips=True
    )
    req = urllib.request.Request("https://example.com/1")
    with mock.patch.object(web, "_host_is_public", return_value=True):
        redirected = handler.redirect_request(
            req, None, 302, "Found", {}, "https://example.com/2"
        )
    assert redirected is not None
    assert redirected.full_url == "https://example.com/2"


# --- SSRF peer verification -------------------------------------------------


def test_verify_public_peer_blocks_private() -> None:
    """A connection to a private peer is rejected after connect."""
    fake_resp = mock.Mock()
    fake_resp.fp.raw._sock.getpeername.return_value = ("127.0.0.1", 80)
    with pytest.raises(web._HttpError, match="SSRF"):
        web._verify_public_peer(fake_resp)


def test_verify_public_peer_allows_public() -> None:
    """A connection to a public peer passes the post-connect check."""
    fake_resp = mock.Mock()
    fake_resp.fp.raw._sock.getpeername.return_value = ("93.184.216.34", 443)
    web._verify_public_peer(fake_resp)


def test_verify_public_peer_skips_unintrospectable() -> None:
    """Responses that hide the socket (or mocks) skip the check silently."""
    web._verify_public_peer(mock.MagicMock())


# --- fetch_url: Jina Reader fallback ----------------------------------------


def _jina_markdown_body() -> bytes:
    return (
        b"Title: Acme Careers\n"
        b"URL Source: https://acme.com/careers\n"
        b"Markdown Content:\n"
        b"# Join us\n"
        b"We hire engineers.\n"
    )


def test_fetch_url_jina_fallback_on_empty() -> None:
    """Empty direct extraction triggers the Jina Reader fallback."""
    direct_opener = make_fetch_opener(
        body=b"<html><body></body></html>", url="https://acme.com/careers"
    )
    jina_opener = make_fetch_opener(
        body=_jina_markdown_body(), url="https://r.jina.ai/https://acme.com/careers"
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener",
            side_effect=[direct_opener, jina_opener],
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value=""),
    ):
        result = client.fetch_url("https://acme.com/careers")

    assert isinstance(result, PageContent)
    assert result.source == "jina"
    assert result.url == "https://acme.com/careers"
    assert result.title == "Acme Careers"
    assert "We hire engineers" in result.text


def test_fetch_url_jina_fallback_on_js_heavy() -> None:
    """Known JS-heavy boards go straight to Jina; the plain fetch is skipped."""
    jina_opener = make_fetch_opener(
        body=_jina_markdown_body(),
        url="https://r.jina.ai/https://www.linkedin.com/jobs/1",
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener", return_value=jina_opener
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
    ):
        result = client.fetch_url("https://www.linkedin.com/jobs/1")

    assert isinstance(result, PageContent)
    assert result.source == "jina"
    jina_opener.open.assert_called_once()


def test_fetch_url_jina_sends_api_key_header() -> None:
    """When JINA_API_KEY is set, the Jina request carries a Bearer header."""
    jina_opener = make_fetch_opener(
        body=_jina_markdown_body(),
        url="https://r.jina.ai/https://www.linkedin.com/jobs/1",
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener", return_value=jina_opener
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.dict(os.environ, {"JINA_API_KEY": "jina_test_secret"}, clear=False),
    ):
        result = client.fetch_url("https://www.linkedin.com/jobs/1")

    assert isinstance(result, PageContent)
    assert result.source == "jina"
    request = jina_opener.open.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer jina_test_secret"


def test_fetch_url_jina_no_key_sends_no_auth() -> None:
    """Without JINA_API_KEY the Jina request carries no Authorization header."""
    jina_opener = make_fetch_opener(
        body=_jina_markdown_body(),
        url="https://r.jina.ai/https://www.linkedin.com/jobs/1",
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener", return_value=jina_opener
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.dict(os.environ, {}, clear=True),
    ):
        result = client.fetch_url("https://www.linkedin.com/jobs/1")

    assert isinstance(result, PageContent)
    request = jina_opener.open.call_args[0][0]
    assert request.get_header("Authorization") is None


def test_fetch_url_jina_skipped_when_js_heavy_and_disabled() -> None:
    """With Jina disabled, JS-heavy boards are fetched plainly."""
    direct_opener = make_fetch_opener(
        body=b"<html><title>LinkedIn</title></html>",
        url="https://www.linkedin.com/jobs/1",
    )
    client = make_client(use_jina_reader=False)

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener", return_value=direct_opener
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value="Direct text"),
    ):
        result = client.fetch_url("https://www.linkedin.com/jobs/1")

    assert isinstance(result, PageContent)
    assert result.source == "direct"
    direct_opener.open.assert_called_once()


def test_fetch_url_jina_disabled() -> None:
    """With use_jina_reader off, empty extraction returns the direct error."""
    direct_opener = make_fetch_opener(
        body=b"<html><body></body></html>", url="https://acme.com/careers"
    )
    client = make_client(use_jina_reader=False)

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener",
            return_value=direct_opener,
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value=""),
    ):
        result = client.fetch_url("https://acme.com/careers")

    assert isinstance(result, dict)
    assert "No readable content" in result["error"]


def test_fetch_url_jina_cap() -> None:
    """Jina fallback fetches stop once max_jina_calls is exhausted."""
    resp1 = make_fetch_opener(
        body=b"<html></html>", url="https://acme.com/1"
    ).open.return_value
    resp2 = make_fetch_opener(
        body=_jina_markdown_body(), url="https://r.jina.ai/https://acme.com/1"
    ).open.return_value
    resp3 = make_fetch_opener(
        body=b"<html></html>", url="https://acme.com/2"
    ).open.return_value
    opener = mock.MagicMock()
    opener.open.side_effect = [resp1, resp2, resp3]
    client = make_client(max_jina_calls=1)

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value=""),
    ):
        client.fetch_url("https://acme.com/1")
        result = client.fetch_url("https://acme.com/2")

    assert isinstance(result, dict)
    requested = [call[0][0].full_url for call in opener.open.call_args_list]
    assert requested == [
        "https://acme.com/1",
        "https://r.jina.ai/https://acme.com/1",
        "https://acme.com/2",
    ]


def test_fetch_url_jina_fallback_to_direct_on_failure() -> None:
    """JS-heavy boards fall back to the plain fetch when Jina fails."""
    failing_jina_opener = mock.MagicMock()
    failing_jina_opener.open.side_effect = urllib.error.HTTPError(
        url="https://r.jina.ai/", code=500, msg="Server Error", hdrs=None, fp=None
    )
    direct_opener = make_fetch_opener(
        body=b"<html><title>LinkedIn</title></html>",
        url="https://www.linkedin.com/jobs/1",
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener",
            side_effect=[failing_jina_opener, direct_opener],
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value="Direct text"),
    ):
        result = client.fetch_url("https://www.linkedin.com/jobs/1")

    assert isinstance(result, PageContent)
    assert result.source == "direct"
    assert result.text == "Direct text"


def test_fetch_url_jina_failure_returns_direct_error() -> None:
    """When Jina fails on an empty direct fetch, the direct error is returned."""
    direct_opener = make_fetch_opener(body=b"<html></html>", url="https://acme.com/1")
    failing_jina_opener = mock.MagicMock()
    failing_jina_opener.open.side_effect = urllib.error.HTTPError(
        url="https://r.jina.ai/", code=500, msg="Server Error", hdrs=None, fp=None
    )
    client = make_client()

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener",
            side_effect=[direct_opener, failing_jina_opener],
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value=""),
    ):
        result = client.fetch_url("https://acme.com/1")

    assert isinstance(result, dict)
    assert "No readable content" in result["error"]


def test_fetch_url_jina_cap_zero() -> None:
    """With max_jina_calls=0, the Jina fallback is never attempted."""
    direct_opener = make_fetch_opener(body=b"<html></html>", url="https://acme.com/1")
    client = make_client(max_jina_calls=0)

    with (
        mock.patch(
            "jobgitops.web.urllib.request.build_opener", return_value=direct_opener
        ),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value=""),
    ):
        result = client.fetch_url("https://acme.com/1")

    assert isinstance(result, dict)
    assert "No readable content" in result["error"]
    direct_opener.open.assert_called_once()


def test_parse_jina_body_variants() -> None:
    """Jina body parsing handles standard, and non-Jina bodies."""
    title, text = web._parse_jina_body(
        "Title: Acme\nURL Source: https://acme.com\nMarkdown Content:\n# Hello"
    )
    assert title == "Acme"
    assert text == "# Hello"
    title, text = web._parse_jina_body("No markers here")
    assert title == ""
    assert text == "No markers here"


# --- fetch_url: memoization -------------------------------------------------


def test_fetch_url_memoization() -> None:
    """Identical URL fetches within one run execute exactly once."""
    opener = make_fetch_opener(body=b"<html><title>T</title></html>")
    client = make_client()

    with (
        mock.patch("jobgitops.web.urllib.request.build_opener", return_value=opener),
        mock.patch.object(web, "_host_is_public", return_value=True),
        mock.patch.object(web, "_extract_text", return_value="text"),
    ):
        first = client.fetch_url("https://example.com/a")
        second = client.fetch_url("https://example.com/a")

    assert first == second
    assert opener.open.call_count == 1
