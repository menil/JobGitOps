"""Unit tests for GitHub client wrapper."""

import json
import urllib.error
from unittest import mock

import pytest

from jobgitops.github_client import (
    GitHubClient,
    GitHubClientError,
    extract_label_names,
)


def make_mock_response(
    status: int = 200, reason: str = "OK", body: bytes = b"{}"
) -> mock.MagicMock:
    """Create a mock HTTP response compatible with context manager."""
    mock_resp = mock.MagicMock()
    mock_resp.status = status
    mock_resp.reason = reason
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_extract_label_names() -> None:
    """Test extracting label names filters malformed entries."""
    labels_raw = [
        {"name": "applied"},
        {"name": "interviewing"},
        "not-a-dict",
        {"no-name-key": True},
    ]
    assert extract_label_names(labels_raw) == ["applied", "interviewing"]


def test_github_client_init_validation() -> None:
    """Test repository name format validation on initialization."""
    # Valid formats
    GitHubClient(token="token", repo="owner/repo")
    GitHubClient(token="token", repo="owner-name/repo.name_under")

    # Invalid formats
    with pytest.raises(ValueError) as exc_info:
        GitHubClient(token="token", repo="invalid-repo-format")
    assert "Invalid repository format" in str(exc_info.value)

    with pytest.raises(ValueError):
        GitHubClient(token="token", repo="owner/repo/extra")


def test_github_client_repr() -> None:
    """Test secure __repr__ string representation redacting token."""
    client = GitHubClient(token="secret-12345", repo="owner/repo")
    repr_str = repr(client)
    assert "owner/repo" in repr_str
    assert "secret-12345" not in repr_str
    assert "***" in repr_str


@mock.patch("urllib.request.urlopen")
def test_post_comment(mock_urlopen: mock.MagicMock) -> None:
    """Test posting a comment to an issue."""
    expected_response = {"id": 123, "body": "My comment"}
    resp_body = json.dumps(expected_response).encode("utf-8")
    mock_urlopen.return_value = make_mock_response(status=201, body=resp_body)

    client = GitHubClient(token="my-token", repo="owner/repo", timeout=12)
    res = client.post_comment(issue_number=42, body="My comment")

    assert res == expected_response
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    timeout = mock_urlopen.call_args[1]["timeout"]
    assert timeout == 12
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42/comments"
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer my-token"
    assert json.loads(req.data.decode("utf-8")) == {"body": "My comment"}


@mock.patch("urllib.request.urlopen")
def test_add_labels(mock_urlopen: mock.MagicMock) -> None:
    """Test adding labels to an issue."""
    expected_response = [{"name": "label-A"}, {"name": "label-B"}]
    resp_body = json.dumps(expected_response).encode("utf-8")
    mock_urlopen.return_value = make_mock_response(status=200, body=resp_body)

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.add_labels(issue_number=42, labels=["label-A", "label-B"])

    assert res == ["label-A", "label-B"]
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42/labels"
    assert req.method == "POST"
    assert json.loads(req.data.decode("utf-8")) == {"labels": ["label-A", "label-B"]}


@mock.patch("urllib.request.urlopen")
def test_remove_label_success(mock_urlopen: mock.MagicMock) -> None:
    """Test removing an existing label from an issue with quoting."""
    mock_urlopen.return_value = make_mock_response(status=200, body=b"{}")

    client = GitHubClient(token="my-token", repo="owner/repo")
    client.remove_label(issue_number=42, label="ready to apply")

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    # Slashes and spaces should be unescaped/escaped correctly using percent encoding
    assert (
        req.full_url
        == "https://api.github.com/repos/owner/repo/issues/42/labels/ready%20to%20apply"
    )
    assert req.method == "DELETE"


@mock.patch("urllib.request.urlopen")
def test_remove_label_404_graceful(mock_urlopen: mock.MagicMock) -> None:
    """Test removing a non-existent label returns gracefully instead of throwing."""
    mock_err_fp = mock.MagicMock()
    mock_err_fp.read.return_value = b'{"message": "Label does not exist"}'
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.github.com",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=mock_err_fp,
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    # Should not raise exception
    client.remove_label(issue_number=42, label="missing-label")
    assert mock_urlopen.call_count == 1


@mock.patch("urllib.request.urlopen")
def test_remove_label_other_error_raises(mock_urlopen: mock.MagicMock) -> None:
    """Test removing label with other HTTP error (e.g. 500) raises exception."""
    mock_err_fp = mock.MagicMock()
    mock_err_fp.read.return_value = b'{"message": "Server Error"}'
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.github.com",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=mock_err_fp,
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.remove_label(issue_number=42, label="some-label")

    assert exc_info.value.status_code == 500


@mock.patch("urllib.request.urlopen")
def test_close_issue(mock_urlopen: mock.MagicMock) -> None:
    """Test closing a GitHub issue."""
    expected_response = {"number": 42, "state": "closed"}
    resp_body = json.dumps(expected_response).encode("utf-8")
    mock_urlopen.return_value = make_mock_response(status=200, body=resp_body)

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.close_issue(issue_number=42)

    assert res == expected_response
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42"
    assert req.method == "PATCH"
    assert json.loads(req.data.decode("utf-8")) == {"state": "closed"}


@mock.patch("urllib.request.urlopen")
def test_list_comments(mock_urlopen: mock.MagicMock) -> None:
    """Test listing comments on an issue."""
    expected_response = [
        {"id": 1, "body": "First comment"},
        {"id": 2, "body": "Second comment"},
    ]
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps(expected_response).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.list_comments(issue_number=42)

    assert res == expected_response
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert (
        req.full_url
        == "https://api.github.com/repos/owner/repo/issues/42/comments?per_page=100"
    )
    assert req.method == "GET"


@mock.patch("urllib.request.urlopen")
def test_list_comments_pagination(mock_urlopen: mock.MagicMock) -> None:
    """Test listing comments with pagination parameters."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps([{"id": 1, "body": "Comment"}]).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.list_comments(issue_number=42, per_page=50, page=2)

    assert res == [{"id": 1, "body": "Comment"}]
    req = mock_urlopen.call_args[0][0]
    assert (
        req.full_url
        == "https://api.github.com/repos/owner/repo/issues/42/comments?per_page=50&page=2"
    )
    assert req.method == "GET"


@mock.patch("urllib.request.urlopen")
def test_list_comments_invalid_response_format(mock_urlopen: mock.MagicMock) -> None:
    """Test list_comments raising error on non-list JSON response."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=b'{"error": "not a list"}'
    )
    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.list_comments(issue_number=42)
    assert "Unexpected response format" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_get_labels(mock_urlopen: mock.MagicMock) -> None:
    """Test getting the label names on an issue."""
    issue_data = {
        "number": 42,
        "title": "Job",
        "labels": [{"name": "applied"}, {"name": "interviewing"}],
    }
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps(issue_data).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.get_labels(issue_number=42)

    assert res == ["applied", "interviewing"]
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42"
    assert req.method == "GET"


@mock.patch("urllib.request.urlopen")
def test_get_labels_empty(mock_urlopen: mock.MagicMock) -> None:
    """Test get_labels on an issue without labels returns an empty list."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps({"number": 42, "labels": []}).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    assert client.get_labels(issue_number=42) == []


@mock.patch("urllib.request.urlopen")
def test_list_issues(mock_urlopen: mock.MagicMock) -> None:
    """Test listing issues from repository."""
    expected_response = [
        {"number": 1, "title": "Job 1"},
        {"number": 2, "title": "Job 2"},
    ]
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps(expected_response).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.list_issues(state="open", per_page=50)

    assert res == expected_response
    req = mock_urlopen.call_args[0][0]
    assert (
        req.full_url
        == "https://api.github.com/repos/owner/repo/issues?state=open&per_page=50"
    )
    assert req.method == "GET"


@mock.patch("urllib.request.urlopen")
def test_list_issues_with_labels_filter(mock_urlopen: mock.MagicMock) -> None:
    """Test listing issues filtered by a comma-separated label list."""
    expected_response = [{"number": 1, "title": "Job 1"}]
    mock_urlopen.return_value = make_mock_response(
        status=200, body=json.dumps(expected_response).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.list_issues(state="open", labels="triage-pending")

    assert res == expected_response
    req = mock_urlopen.call_args[0][0]
    assert (
        req.full_url == "https://api.github.com/repos/owner/repo/issues"
        "?state=open&per_page=100&labels=triage-pending"
    )
    assert req.method == "GET"


@mock.patch("urllib.request.urlopen")
def test_create_issue(mock_urlopen: mock.MagicMock) -> None:
    """Test creating a new issue with labels."""
    expected_response = {"number": 101, "title": "New Job"}
    mock_urlopen.return_value = make_mock_response(
        status=201, body=json.dumps(expected_response).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    res = client.create_issue(
        title="New Job", body="Details...", labels=["triage-pending"]
    )

    assert res == expected_response
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues"
    assert req.method == "POST"
    assert json.loads(req.data.decode("utf-8")) == {
        "title": "New Job",
        "body": "Details...",
        "labels": ["triage-pending"],
    }


@mock.patch("urllib.request.urlopen")
def test_api_http_error(mock_urlopen: mock.MagicMock) -> None:
    """Test handling of HTTP errors from the GitHub API."""
    mock_err_fp = mock.MagicMock()
    mock_err_fp.read.return_value = b'{"message": "Not Found"}'
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.github.com",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=mock_err_fp,
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.close_issue(issue_number=42)

    assert "GitHub API request failed: 404 Not Found" in str(exc_info.value)
    assert '{"message": "Not Found"}' in str(exc_info.value)
    assert exc_info.value.status_code == 404
    assert exc_info.value.response_body == '{"message": "Not Found"}'


@mock.patch("urllib.request.urlopen")
def test_api_other_error(mock_urlopen: mock.MagicMock) -> None:
    """Test handling of general exceptions during API communication."""
    mock_urlopen.side_effect = Exception("Connection reset")

    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.close_issue(issue_number=42)

    assert "GitHub API communication failed: Connection reset" in str(exc_info.value)


def test_update_project_status_no_project_id() -> None:
    """Test update_project_status returns immediately if project_id is not set."""
    client = GitHubClient(token="my-token", repo="owner/repo", project_id=None)
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        client.update_project_status("issue-node-id", "Applied")
        mock_urlopen.assert_not_called()


@mock.patch("urllib.request.urlopen")
def test_update_project_status_success_and_caching(
    mock_urlopen: mock.MagicMock,
) -> None:
    """Test successful project status transition flow and verifying schema cache."""
    # First Status Update Flow (3 network requests)
    # 1. Add item response
    resp_add = {"data": {"addProjectV2ItemById": {"item": {"id": "item-id-123"}}}}
    # 2. Get project fields response
    resp_fields = {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "field-id-456",
                            "name": "Status",
                            "options": [
                                {"id": "opt-id-1", "name": "Ready to Apply"},
                                {"id": "opt-id-2", "name": "Applied"},
                            ],
                        }
                    ]
                }
            }
        }
    }
    # 3. Update field value response
    resp_update = {
        "data": {
            "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-id-123"}}
        }
    }

    # Second Status Update Flow (Uses cache; only 2 network requests)
    # 1. Add item response (returns same item ID)
    resp_add_2 = {"data": {"addProjectV2ItemById": {"item": {"id": "item-id-123"}}}}
    # 2. Update field value response
    resp_update_2 = {
        "data": {
            "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-id-123"}}
        }
    }

    mock_urlopen.side_effect = [
        make_mock_response(body=json.dumps(resp_add).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_fields).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_update).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_add_2).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_update_2).encode("utf-8")),
    ]

    client = GitHubClient(
        token="my-token",
        repo="owner/repo",
        project_id="project-node-id",
        status_field_name="Status",
    )

    # 1st call: should request add item, query fields, and update field
    client.update_project_status("issue-node-id", "Applied")
    assert mock_urlopen.call_count == 3

    # 2nd call: should request add item, use cached fields, and update field
    # (skipping query fields request)
    client.update_project_status("issue-node-id", "Ready to Apply")
    assert mock_urlopen.call_count == 5

    calls = mock_urlopen.call_args_list

    # Verify 1st call Add mutation
    req1 = calls[0][0][0]
    body1 = json.loads(req1.data.decode("utf-8"))
    assert "addProjectV2ItemById" in body1["query"]

    # Verify 1st call Get Fields query
    req2 = calls[1][0][0]
    body2 = json.loads(req2.data.decode("utf-8"))
    assert "GetProjectFields" in body2["query"]

    # Verify 1st call Update Field Value mutation
    req3 = calls[2][0][0]
    body3 = json.loads(req3.data.decode("utf-8"))
    assert body3["variables"]["optionId"] == "opt-id-2"

    # Verify 2nd call Add mutation
    req4 = calls[3][0][0]
    body4 = json.loads(req4.data.decode("utf-8"))
    assert "addProjectV2ItemById" in body4["query"]

    # Verify 2nd call Update Field Value mutation (skips field queries,
    # calls update directly)
    req5 = calls[4][0][0]
    body5 = json.loads(req5.data.decode("utf-8"))
    assert "updateProjectV2ItemFieldValue" in body5["query"]
    assert body5["variables"]["optionId"] == "opt-id-1"


@mock.patch("urllib.request.urlopen")
def test_update_project_status_graphql_errors(mock_urlopen: mock.MagicMock) -> None:
    """Test GraphQL error handling during project update."""
    resp_err = {"errors": [{"message": "Some GraphQL error"}]}
    mock_urlopen.return_value = make_mock_response(
        body=json.dumps(resp_err).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo", project_id="proj-id")
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "GraphQL error adding item to project" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_update_project_status_missing_item_id(mock_urlopen: mock.MagicMock) -> None:
    """Test handling when response is missing item ID."""
    resp_empty = {"data": {}}
    mock_urlopen.return_value = make_mock_response(
        body=json.dumps(resp_empty).encode("utf-8")
    )

    client = GitHubClient(token="my-token", repo="owner/repo", project_id="proj-id")
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "Failed to add or retrieve item ID from project" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_update_project_status_field_not_found(mock_urlopen: mock.MagicMock) -> None:
    """Test status update when target status field is missing from project."""
    resp_add = {"data": {"addProjectV2ItemById": {"item": {"id": "item-123"}}}}
    resp_fields = {
        "data": {
            "node": {"fields": {"nodes": [{"id": "field-1", "name": "Other Field"}]}}
        }
    }

    mock_urlopen.side_effect = [
        make_mock_response(body=json.dumps(resp_add).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_fields).encode("utf-8")),
    ]

    client = GitHubClient(
        token="my-token",
        repo="owner/repo",
        project_id="proj-id",
        status_field_name="Status",
    )
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "Status field 'Status' not found in project" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_update_project_status_option_not_found(mock_urlopen: mock.MagicMock) -> None:
    """Test status update when target status value/option is missing."""
    resp_add = {"data": {"addProjectV2ItemById": {"item": {"id": "item-123"}}}}
    resp_fields = {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "field-status",
                            "name": "Status",
                            "options": [{"id": "opt-1", "name": "Ready to Apply"}],
                        }
                    ]
                }
            }
        }
    }

    mock_urlopen.side_effect = [
        make_mock_response(body=json.dumps(resp_add).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_fields).encode("utf-8")),
    ]

    client = GitHubClient(
        token="my-token",
        repo="owner/repo",
        project_id="proj-id",
        status_field_name="Status",
    )
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "Status option 'Applied' not found in status field options" in str(
        exc_info.value
    )


@mock.patch("urllib.request.urlopen")
def test_custom_headers(mock_urlopen: mock.MagicMock) -> None:
    """Test passing custom headers to _request."""
    mock_urlopen.return_value = make_mock_response(status=200, body=b"{}")
    client = GitHubClient(token="my-token", repo="owner/repo")
    client._request(
        "GET",
        "https://api.github.com/test",
        headers={"X-Custom-Header": "value"},
    )
    req = mock_urlopen.call_args[0][0]
    assert req.headers["X-custom-header"] == "value"


@mock.patch("urllib.request.urlopen")
def test_api_http_error_read_exception(mock_urlopen: mock.MagicMock) -> None:
    """Test handling of HTTP errors when reading error body fails."""
    mock_err_fp = mock.MagicMock()
    mock_err_fp.read.side_effect = Exception("Read failed")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.github.com",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=mock_err_fp,
    )

    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.close_issue(issue_number=42)

    assert "GitHub API request failed: 500 Internal Server Error" in str(exc_info.value)
    assert "No detailed body" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_post_comment_invalid_response_format(mock_urlopen: mock.MagicMock) -> None:
    """Test post_comment raising error on non-dict JSON response."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=b'["not", "a", "dict"]'
    )
    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.post_comment(issue_number=42, body="comment")
    assert "Unexpected response format" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_add_labels_invalid_response_format(mock_urlopen: mock.MagicMock) -> None:
    """Test add_labels raising error on non-list JSON response."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=b'{"error": "not a list"}'
    )
    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.add_labels(issue_number=42, labels=["label1"])
    assert "Unexpected response format" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_close_issue_invalid_response_format(mock_urlopen: mock.MagicMock) -> None:
    """Test close_issue raising error on non-dict JSON response."""
    mock_urlopen.return_value = make_mock_response(
        status=200, body=b'["not", "a", "dict"]'
    )
    client = GitHubClient(token="my-token", repo="owner/repo")
    with pytest.raises(GitHubClientError) as exc_info:
        client.close_issue(issue_number=42)
    assert "Unexpected response format" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_update_project_status_graphql_fields_error(
    mock_urlopen: mock.MagicMock,
) -> None:
    """Test GraphQL error during project fields query."""
    resp_add = {"data": {"addProjectV2ItemById": {"item": {"id": "item-123"}}}}
    resp_fields_err = {"errors": [{"message": "Fields query error"}]}

    mock_urlopen.side_effect = [
        make_mock_response(body=json.dumps(resp_add).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_fields_err).encode("utf-8")),
    ]

    client = GitHubClient(token="my-token", repo="owner/repo", project_id="proj-id")
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "GraphQL error fetching project fields" in str(exc_info.value)


@mock.patch("urllib.request.urlopen")
def test_update_project_status_graphql_update_error(
    mock_urlopen: mock.MagicMock,
) -> None:
    """Test GraphQL error during status field value update mutation."""
    resp_add = {"data": {"addProjectV2ItemById": {"item": {"id": "item-123"}}}}
    resp_fields = {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "field-1",
                            "name": "Status",
                            "options": [{"id": "opt-1", "name": "Applied"}],
                        }
                    ]
                }
            }
        }
    }
    resp_update_err = {"errors": [{"message": "Update status error"}]}

    mock_urlopen.side_effect = [
        make_mock_response(body=json.dumps(resp_add).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_fields).encode("utf-8")),
        make_mock_response(body=json.dumps(resp_update_err).encode("utf-8")),
    ]

    client = GitHubClient(token="my-token", repo="owner/repo", project_id="proj-id")
    with pytest.raises(GitHubClientError) as exc_info:
        client.update_project_status("issue-node-id", "Applied")

    assert "GraphQL error updating project status" in str(exc_info.value)
