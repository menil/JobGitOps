"""Unit tests for the Gmail API client wrapper."""

import base64
from unittest import mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gitemployed.gmail_client import (
    GmailClient,
    GmailClientError,
    GmailMessageNotFoundError,
    Message,
    build_gmail_permalink,
    is_authentic,
)

TRUSTED_ID = "mx.google.com"


def make_http_error(status: int) -> HttpError:
    """Build a real `HttpError` carrying the given HTTP status code."""
    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp, b'{"error": {"message": "boom"}}')


def _b64url(text: str) -> str:
    """Base64url-encode `text` the way the Gmail API encodes body data."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def auth_result(authserv_id: str, dmarc: str | None) -> str:
    """Build a realistic `Authentication-Results` header value."""
    base = (
        f"{authserv_id};\n"
        "       dkim=pass header.i=@example.com header.s=default "
        "header.b=abc;\n"
        "       spf=pass (google.com: domain of hr@example.com designates "
        "1.2.3.4 as permitted sender) smtp.mailfrom=hr@example.com"
    )
    if dmarc is None:
        return base
    return f"{base};\n       dmarc={dmarc} (p=REJECT) header.from=example.com"


@pytest.fixture
def gmail_client() -> tuple[GmailClient, mock.MagicMock]:
    """A `GmailClient` with `Credentials`/`build` mocked out, plus the
    resulting mocked service object for request-shape assertions."""
    with (
        mock.patch("gitemployed.gmail_client.Credentials") as mock_credentials,
        mock.patch("gitemployed.gmail_client.build") as mock_build,
    ):
        mock_service = mock.MagicMock()
        mock_build.return_value = mock_service
        client = GmailClient(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )
        yield client, mock_service, mock_credentials, mock_build


def test_init_builds_credentials_and_service(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """Credentials and the Gmail service are built from the three secrets,
    with no manual token-refresh call anywhere in __init__."""
    client, mock_service, mock_credentials, mock_build = gmail_client

    mock_credentials.assert_called_once_with(
        token=None,
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
    )
    mock_build.assert_called_once_with(
        "gmail",
        "v1",
        credentials=mock_credentials.return_value,
        static_discovery=True,
    )
    assert client._service is mock_service
    mock_credentials.return_value.refresh.assert_not_called()


def test_resolve_label_id_found(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """An exact, case-sensitive label name match returns its ID."""
    client, mock_service, _, _ = gmail_client
    labels_resource = mock_service.users.return_value.labels.return_value
    labels_resource.list.return_value.execute.return_value = {
        "labels": [
            {"id": "Label_1", "name": "Job Applications"},
            {"id": "Label_2", "name": "job applications"},
        ]
    }

    assert client.resolve_label_id("Job Applications") == "Label_1"
    labels_resource.list.assert_called_once_with(userId="me")


def test_resolve_label_id_not_found(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """No matching label name returns None rather than raising."""
    client, mock_service, _, _ = gmail_client
    labels_resource = mock_service.users.return_value.labels.return_value
    labels_resource.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Other Label"}]
    }

    assert client.resolve_label_id("Job Applications") is None


def test_resolve_label_id_case_sensitive(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """A differently-cased label name is not treated as a match."""
    client, mock_service, _, _ = gmail_client
    labels_resource = mock_service.users.return_value.labels.return_value
    labels_resource.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_2", "name": "job applications"}]
    }

    assert client.resolve_label_id("Job Applications") is None


@pytest.mark.parametrize(
    ("query", "expected_q"),
    [
        (None, "newer_than:7d"),
        ("from:ats@example.com", "newer_than:7d from:ats@example.com"),
    ],
)
def test_list_message_ids_request_shape(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
    query: str | None,
    expected_q: str,
) -> None:
    """The label is scoped via labelIds, and newer_than/query build the q
    string, with no nextPageToken so pagination stops after one page."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    request = mock.MagicMock()
    messages_resource.list.return_value = request
    request.execute.return_value = {"messages": [{"id": "m1"}]}
    messages_resource.list_next.return_value = None

    result = client.list_message_ids(label_id="Label_1", query=query, days_back=7)

    assert result == ["m1"]
    messages_resource.list.assert_called_once_with(
        userId="me", labelIds=["Label_1"], q=expected_q
    )


def test_list_message_ids_pagination(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """list_message_ids follows list_next() across multiple pages until it
    returns None, collecting message IDs from every page."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value

    first_request = mock.MagicMock(name="first_request")
    second_request = mock.MagicMock(name="second_request")
    first_response = {
        "messages": [{"id": "m1"}, {"id": "m2"}],
        "nextPageToken": "token-1",
    }
    second_response = {"messages": [{"id": "m3"}]}

    messages_resource.list.return_value = first_request
    first_request.execute.return_value = first_response
    second_request.execute.return_value = second_response
    messages_resource.list_next.side_effect = [second_request, None]

    result = client.list_message_ids(label_id="Label_1", query=None, days_back=30)

    assert result == ["m1", "m2", "m3"]
    assert messages_resource.list_next.call_args_list == [
        mock.call(first_request, first_response),
        mock.call(second_request, second_response),
    ]


def test_list_message_ids_no_messages_key(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """An empty mailbox response (no "messages" key at all) yields []."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    request = mock.MagicMock()
    messages_resource.list.return_value = request
    request.execute.return_value = {}
    messages_resource.list_next.return_value = None

    assert client.list_message_ids("Label_1", None, 7) == []


def _full_message_response(payload: dict) -> dict:
    return {"id": "msg-1", "payload": payload}


def test_get_message_single_api_call_headers_and_body(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """get_message makes exactly one API call and derives both the headers
    and the body from that one format=full response."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Date", "value": "Mon, 1 Sep 2026 10:00:00 -0700"},
            {"name": "Subject", "value": "Your application update"},
            {"name": "From", "value": "ATS <no-reply@ats.example.com>"},
            {
                "name": "Authentication-Results",
                "value": auth_result(TRUSTED_ID, "pass"),
            },
        ],
        "body": {"data": _b64url("Hello, world!")},
    }
    messages_resource.get.return_value.execute.return_value = _full_message_response(
        payload
    )

    result = client.get_message("msg-1")

    messages_resource.get.assert_called_once_with(
        userId="me", id="msg-1", format="full"
    )
    messages_resource.get.return_value.execute.assert_called_once_with()
    assert result == Message(
        message_id="msg-1",
        date="Mon, 1 Sep 2026 10:00:00 -0700",
        subject="Your application update",
        sender="ATS <no-reply@ats.example.com>",
        raw_auth_results=[auth_result(TRUSTED_ID, "pass")],
        body_text="Hello, world!",
    )


def test_get_message_prefers_text_plain_over_html(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """When a message has both text/plain and text/html parts, text/plain
    wins."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [],
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64url("Plain text")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64url("<b>HTML</b>")},
            },
        ],
    }
    messages_resource.get.return_value.execute.return_value = _full_message_response(
        payload
    )

    result = client.get_message("msg-1")

    assert result.body_text == "Plain text"


def test_get_message_falls_back_to_stripped_html(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """When only text/html is present, the body is tag-stripped and its
    entities unescaped."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    html_body = "<p>Thanks &amp; good luck, <b>Jordan</b>!</p>"
    encoded = _b64url(html_body)
    payload = {
        "mimeType": "text/html",
        "headers": [],
        "body": {"data": encoded},
    }
    messages_resource.get.return_value.execute.return_value = _full_message_response(
        payload
    )

    result = client.get_message("msg-1")

    assert result.body_text == "Thanks & good luck, Jordan!"


def test_get_message_no_body_parts_yields_empty_string(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """A message with neither a text/plain nor a text/html part (e.g. an
    empty body) yields an empty body_text rather than raising."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    payload = {"mimeType": "text/calendar", "headers": [], "body": {}}
    messages_resource.get.return_value.execute.return_value = _full_message_response(
        payload
    )

    result = client.get_message("msg-1")

    assert result.body_text == ""


def test_get_message_recurses_nested_multipart(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
) -> None:
    """A multipart/mixed message wrapping a multipart/alternative part (plus
    an unrelated attachment part) is recursed correctly to find the
    text/plain body."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    plain_text = "Nested plain body"
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64url(plain_text)},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64url("<p>Nested HTML</p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "resume.pdf",
                "body": {"attachmentId": "abc123"},
            },
        ],
    }
    messages_resource.get.return_value.execute.return_value = _full_message_response(
        payload
    )

    result = client.get_message("msg-1")

    assert result.body_text == plain_text


@pytest.mark.parametrize(
    ("raw_auth_results", "expected"),
    [
        pytest.param([auth_result(TRUSTED_ID, "pass")], True, id="single-pass"),
        pytest.param([auth_result(TRUSTED_ID, "fail")], False, id="single-fail"),
        pytest.param([], False, id="no-header-at-all"),
        pytest.param(
            [auth_result("suspicious-relay.example", "pass")],
            False,
            id="only-untrusted-header-present",
        ),
        pytest.param(
            [
                auth_result("suspicious-relay.example", "pass"),
                auth_result(TRUSTED_ID, "fail"),
            ],
            False,
            id="spoofing-regression-untrusted-pass-trusted-fail",
        ),
        pytest.param(
            [
                auth_result("suspicious-relay.example", "pass"),
                auth_result(TRUSTED_ID, "pass"),
            ],
            True,
            id="untrusted-pass-and-trusted-pass",
        ),
        pytest.param(
            [auth_result(TRUSTED_ID, None)],
            False,
            id="malformed-missing-dmarc-result",
        ),
        pytest.param(
            [
                auth_result(TRUSTED_ID, "pass"),
                auth_result(TRUSTED_ID, "fail"),
            ],
            False,
            id="ambiguous-two-trusted-instances",
        ),
    ],
)
def test_is_authentic(raw_auth_results: list[str], expected: bool) -> None:
    """is_authentic is pinned to the single Authentication-Results
    instance whose authserv-id matches the trusted boundary."""
    assert is_authentic(raw_auth_results, trusted_authserv_id=TRUSTED_ID) is expected


def test_build_gmail_permalink() -> None:
    """The permalink uses the #all/ fragment so it resolves regardless of
    which label the message is filed under."""
    assert (
        build_gmail_permalink("18c9f0a1b2c3d4e5")
        == "https://mail.google.com/mail/u/0/#all/18c9f0a1b2c3d4e5"
    )


@pytest.mark.parametrize(
    ("method_name", "status_code"),
    [
        ("resolve_label_id", 403),
        ("resolve_label_id", 500),
        ("list_message_ids", 403),
        ("list_message_ids", 500),
    ],
)
def test_http_error_propagates_as_client_error(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
    method_name: str,
    status_code: int,
) -> None:
    """A non-404 HttpError from labels/messages list() surfaces as a real
    GmailClientError failure rather than being swallowed."""
    client, mock_service, _, _ = gmail_client
    labels_resource = mock_service.users.return_value.labels.return_value
    labels_resource.list.return_value.execute.side_effect = make_http_error(status_code)
    messages_resource = mock_service.users.return_value.messages.return_value
    messages_resource.list.return_value.execute.side_effect = make_http_error(
        status_code
    )
    messages_resource.list_next.return_value = None

    with pytest.raises(GmailClientError) as exc_info:
        if method_name == "resolve_label_id":
            client.resolve_label_id("Job Applications")
        else:
            client.list_message_ids("Label_1", None, 7)

    assert exc_info.value.status_code == status_code


@pytest.mark.parametrize(
    ("status_code", "expected_exception"),
    [
        (404, GmailMessageNotFoundError),
        (403, GmailClientError),
        (500, GmailClientError),
    ],
)
def test_get_message_http_error_table(
    gmail_client: tuple[GmailClient, mock.MagicMock, mock.MagicMock, mock.MagicMock],
    status_code: int,
    expected_exception: type[Exception],
) -> None:
    """get_message distinguishes a since-deleted message (404 -> the
    specific, catchable GmailMessageNotFoundError, so a caller can skip
    just that message) from any other failure (403/500 -> the generic
    GmailClientError, which must propagate as a real failure)."""
    client, mock_service, _, _ = gmail_client
    messages_resource = mock_service.users.return_value.messages.return_value
    messages_resource.get.return_value.execute.side_effect = make_http_error(
        status_code
    )

    with pytest.raises(expected_exception) as exc_info:
        client.get_message("msg-1")

    assert exc_info.value.status_code == status_code
    # GmailMessageNotFoundError is itself a GmailClientError, so a caller
    # that only wants to catch the generic failure type still catches it.
    assert isinstance(exc_info.value, GmailClientError)
    if status_code == 404:
        assert not isinstance(GmailClientError("x"), GmailMessageNotFoundError)
