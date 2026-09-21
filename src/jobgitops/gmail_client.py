"""Thin wrapper over the Gmail API (spec `specs/gmail-integration.md` §5.1).

Uses the official `google-api-python-client` (`googleapiclient.discovery.
build`) together with `google-auth` (`google.oauth2.credentials.
Credentials`) for every Gmail call, rather than hand-rolled HTTP. Only two
hosts are ever contacted: `oauth2.googleapis.com` (token refresh, handled
transparently by the credentials object on first request) and
`gmail.googleapis.com` (the API itself) — there is no configurable base URL
by design (spec §9.5), so this module cannot be pointed at any other host.
"""

import base64
import html
import logging
import re
from dataclasses import dataclass, field

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("jobgitops.gmail_client")

# The only two hosts this module ever contacts (spec §9.5): the OAuth token
# endpoint (used transparently by `Credentials` for lazy refresh) and the
# Gmail API itself (reached via `googleapiclient.discovery.build`, whose
# discovery document is bundled statically — see `static_discovery=True`
# below — so `build()` itself makes no third-host network call either).
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Default trusted `authserv-id` for the DMARC check (§5.1.2): Gmail's own
# receiving MTA. Only the `Authentication-Results` header instance stamped
# by this exact authserv-id is ever consulted.
DEFAULT_TRUSTED_AUTHSERV_ID = "mx.google.com"

_DMARC_RESULT_RE = re.compile(r"\bdmarc\s*=\s*([a-zA-Z0-9_-]+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class GmailClientError(Exception):
    """Raised when a Gmail API operation fails for a reason a caller must
    treat as a real failure (auth/quota/server error), not a per-item
    condition it can reasonably skip."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize the Gmail client error.

        Args:
            message: The exception message.
            status_code: The HTTP status code if available.
        """
        super().__init__(message)
        self.status_code = status_code


class GmailMessageNotFoundError(GmailClientError):
    """Raised by `get_message` specifically for HTTP 404: the message was
    deleted (by the user, or by a mail filter) between when its ID was
    listed and when it was fetched. Callers that want to skip a since-
    deleted message rather than treat the whole run as failed can catch
    this specific subclass; every other Gmail API failure raises the base
    `GmailClientError` instead, which is meant to propagate."""


@dataclass
class Message:
    """A single fetched Gmail message, with just the fields this
    integration needs (spec §5.1)."""

    message_id: str
    date: str
    subject: str
    sender: str
    raw_auth_results: list[str] = field(default_factory=list)
    body_text: str = ""


def _http_status(error: HttpError) -> int | None:
    """Best-effort extraction of the HTTP status code from an `HttpError`."""
    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _get_header(headers: list[dict], name: str) -> str:
    """Return the value of the first header matching `name` case-
    insensitively, or an empty string if absent."""
    lowered = name.lower()
    for header in headers:
        if str(header.get("name", "")).lower() == lowered:
            return str(header.get("value", ""))
    return ""


def _get_headers_all(headers: list[dict], name: str) -> list[str]:
    """Return every header value matching `name` case-insensitively, in
    the order they appear (a message can carry more than one instance of
    the same header name, e.g. `Authentication-Results` — spec §5.1.2)."""
    lowered = name.lower()
    return [
        str(header.get("value", ""))
        for header in headers
        if str(header.get("name", "")).lower() == lowered
    ]


def _decode_body_data(data: str) -> str:
    """Decode a Gmail API base64url-encoded MIME part body."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _strip_html(raw_html: str) -> str:
    """Strip HTML tags and unescape entities, for the text/html fallback
    body-extraction path."""
    return html.unescape(_TAG_RE.sub("", raw_html))


def _find_body_parts(payload: dict) -> tuple[str | None, str | None]:
    """Recursively walk a message `payload` (and any nested `parts`) to
    find the first `text/plain` and `text/html` bodies present, preferring
    whichever is encountered first at each level (Gmail structures
    `multipart/alternative` parts in the same document order the sending
    client used, so the first hit at the top level is the intended one)."""
    plain: str | None = None
    html_part: str | None = None

    mime_type = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data:
        if mime_type == "text/plain":
            plain = _decode_body_data(data)
        elif mime_type == "text/html":
            html_part = _decode_body_data(data)

    for part in payload.get("parts") or []:
        part_plain, part_html = _find_body_parts(part)
        if plain is None and part_plain is not None:
            plain = part_plain
        if html_part is None and part_html is not None:
            html_part = part_html

    return plain, html_part


def is_authentic(
    raw_auth_results: list[str],
    trusted_authserv_id: str = DEFAULT_TRUSTED_AUTHSERV_ID,
) -> bool:
    """True only if the `Authentication-Results` header instance whose
    `authserv-id` matches `trusted_authserv_id` reports `dmarc=pass`.

    `Authentication-Results` (RFC 8601) is hop-by-hop and not authenticated
    by default: a message can carry more than one such header (an earlier
    relay can stamp its own before final delivery), and only the instance
    added by Gmail's own trusted receiving MTA is meaningful. Naively
    substring-searching any header instance for "dmarc=pass" would accept a
    header an upstream hop or the sender forged before Gmail ever saw the
    message. This function instead selects the one instance whose
    `authserv-id` identifies the trusted boundary and requires `dmarc=pass`
    specifically there; zero matches, multiple ambiguous matches on that
    `authserv-id`, or a malformed header all fail closed (return False) —
    spec §5.1.2.

    Args:
        raw_auth_results: Every `Authentication-Results` header value found
            on the message, verbatim, in header order.
        trusted_authserv_id: The `authserv-id` identifying the mail
            receiver's own trust boundary. Defaults to Gmail's.

    Returns:
        Whether the trusted authserv-id's own instance reports dmarc=pass.
    """
    trusted_instances = [
        value
        for value in raw_auth_results
        if value.split(";", 1)[0].strip() == trusted_authserv_id
    ]
    if len(trusted_instances) != 1:
        # Zero matches (no trustworthy header at all) or multiple matches
        # (ambiguous — which one is real?) both fail closed.
        return False

    dmarc_match = _DMARC_RESULT_RE.search(trusted_instances[0])
    if dmarc_match is None:
        return False
    return dmarc_match.group(1).lower() == "pass"


def build_gmail_permalink(message_id: str) -> str:
    """Build a Gmail web-UI permalink for `message_id` (spec §5.1.3).

    The `#all/` fragment resolves the message regardless of which label
    it's filed under. This only resolves for someone already signed into
    the Gmail account that owns the message, so unlike quoting the email
    body it's safe to post unconditionally in an issue comment.
    """
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


class GmailClient:
    """Thin wrapper over the Gmail API for the label-scoped mailbox sync
    (spec §5.1). Only ever contacts `oauth2.googleapis.com` and
    `gmail.googleapis.com` — there is no configurable base URL."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        """Build the OAuth credentials and Gmail API service object.

        No manual token-refresh call is made anywhere in this class: the
        client library refreshes the access token transparently and lazily
        on the first request that needs one. An `invalid_grant` error
        (revoked/expired refresh token) surfaces from that first request as
        a `GmailClientError`, not a silent skip.

        Args:
            client_id: OAuth client ID.
            client_secret: OAuth client secret.
            refresh_token: OAuth refresh token for the target mailbox.
        """
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=GMAIL_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
        )
        # static_discovery=True: google-api-python-client v2+ bundles the
        # Gmail discovery document, so this never makes a runtime call to a
        # third host to fetch it (spec §5.1, §9.5).
        self._service = build(
            "gmail", "v1", credentials=credentials, static_discovery=True
        )

    def resolve_label_id(self, label_name: str) -> str | None:
        """Resolve a Gmail label name to its ID via a case-sensitive exact
        match.

        Args:
            label_name: The user-configured Gmail label name.

        Returns:
            The label's ID, or None if no label with that exact name
            exists in the mailbox.
        """
        try:
            response = self._service.users().labels().list(userId="me").execute()
        except HttpError as error:
            raise GmailClientError(
                f"Failed to list Gmail labels: {error}",
                status_code=_http_status(error),
            ) from error

        for label in response.get("labels", []) or []:
            if label.get("name") == label_name:
                return label.get("id")
        return None

    def list_message_ids(
        self, label_id: str, query: str | None, days_back: int
    ) -> list[str]:
        """List every message ID under `label_id` newer than `days_back`
        days, optionally narrowed by `query`.

        The label is scoped via the `labelIds` list() parameter (an exact
        ID match — the correct, unambiguous way to scope by label; no
        quoting/escaping concerns the way embedding a label *name* into a
        free-text `q` string would have). `newer_than:{days_back}d` has no
        parameter-level equivalent, so it's built into `q`, with the
        optional `query` appended when set (spec §5.1.1). Results are
        paginated via the request object's `list_next()` helper until no
        further page remains.

        Args:
            label_id: The Gmail label ID to scope the search to (from
                `resolve_label_id`).
            query: Optional additional free-text query, appended as-is.
            days_back: How many days back to search (`newer_than:{N}d`).

        Returns:
            Every matching message ID, oldest-page-first in the order
            Gmail's API returns them.
        """
        q = f"newer_than:{days_back}d"
        if query:
            q = f"{q} {query}"

        message_ids: list[str] = []
        try:
            request = (
                self._service.users()
                .messages()
                .list(userId="me", labelIds=[label_id], q=q)
            )
            while request is not None:
                response = request.execute()
                message_ids.extend(
                    message["id"] for message in response.get("messages", []) or []
                )
                request = self._service.users().messages().list_next(request, response)
        except HttpError as error:
            raise GmailClientError(
                f"Failed to list Gmail messages: {error}",
                status_code=_http_status(error),
            ) from error

        return message_ids

    def get_message(self, message_id: str) -> Message:
        """Fetch a single message and derive both its headers and body
        from one API call.

        A single `format="full"` call already includes `payload.headers`
        and the full MIME body, so this covers both the DMARC/header check
        and the body text with exactly one Gmail round-trip per message —
        a separate `format="metadata"` pre-fetch would be redundant once
        the body is needed anyway (spec §5.1).

        Args:
            message_id: The Gmail message ID to fetch.

        Returns:
            The parsed `Message`.

        Raises:
            GmailMessageNotFoundError: The message no longer exists
                (HTTP 404) — e.g. deleted between listing and fetching.
            GmailClientError: Any other Gmail API failure.
        """
        try:
            response = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as error:
            status = _http_status(error)
            if status == 404:
                raise GmailMessageNotFoundError(
                    f"Gmail message {message_id} not found", status_code=404
                ) from error
            raise GmailClientError(
                f"Failed to fetch Gmail message {message_id}: {error}",
                status_code=status,
            ) from error

        payload = response.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        plain, html_body = _find_body_parts(payload)
        if plain is not None:
            body_text = plain
        elif html_body is not None:
            body_text = _strip_html(html_body)
        else:
            body_text = ""

        return Message(
            message_id=message_id,
            date=_get_header(headers, "Date"),
            subject=_get_header(headers, "Subject"),
            sender=_get_header(headers, "From"),
            raw_auth_results=_get_headers_all(headers, "Authentication-Results"),
            body_text=body_text,
        )
