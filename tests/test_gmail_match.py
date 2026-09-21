"""Unit tests for Gmail-integration candidate-pool assembly (gmail_match.py)."""

from unittest import mock

from jobgitops.github_client import GitHubClient
from jobgitops.gmail_match import PAGE_SIZE, Candidate, get_candidate_pool


def _issue(
    number: int,
    labels: list[str],
    title: str = "Some Job",
    body: str = "",
) -> dict:
    """Build a minimal fake GitHub issue payload."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


def test_get_candidate_pool_paginates_through_all_open_issues() -> None:
    """Test full pagination is exercised for pools spanning more than one page."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    first_page = [_issue(number, ["applied"]) for number in range(1, PAGE_SIZE + 1)]
    second_page = [_issue(PAGE_SIZE + 1, ["applied"])]
    mock_gh_client.list_issues.side_effect = [first_page, second_page]

    candidates = get_candidate_pool(mock_gh_client)

    assert len(candidates) == PAGE_SIZE + 1
    assert {c.number for c in candidates} == set(range(1, PAGE_SIZE + 2))
    assert mock_gh_client.list_issues.call_args_list == [
        mock.call(state="open", per_page=PAGE_SIZE, page=1),
        mock.call(state="open", per_page=PAGE_SIZE, page=2),
    ]


def test_get_candidate_pool_stops_after_a_short_page() -> None:
    """Test the pagination loop stops as soon as a page is not full."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [_issue(1, ["applied"])]

    candidates = get_candidate_pool(mock_gh_client)

    assert len(candidates) == 1
    mock_gh_client.list_issues.assert_called_once_with(
        state="open", per_page=PAGE_SIZE, page=1
    )


def test_get_candidate_pool_never_uses_server_side_labels_filter() -> None:
    """Regression test: GitHub's labels= is an AND filter across mutually
    exclusive lifecycle labels, so it must never be passed here — the union
    is computed client-side instead (spec §5.3).
    """
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [
        _issue(1, ["ready-to-apply"]),
        _issue(2, ["applied"]),
    ]

    get_candidate_pool(mock_gh_client)

    for call in mock_gh_client.list_issues.call_args_list:
        assert "labels" not in call.kwargs


def test_get_candidate_pool_includes_ready_to_apply() -> None:
    """Test issues labeled ready-to-apply are included in the pool."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [_issue(1, ["ready-to-apply"])]

    candidates = get_candidate_pool(mock_gh_client)

    assert [c.number for c in candidates] == [1]


def test_get_candidate_pool_includes_all_active_lifecycle_labels() -> None:
    """Test applied, in-loop, and offer-received issues are all included."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [
        _issue(1, ["applied"]),
        _issue(2, ["in-loop"]),
        _issue(3, ["offer-received"]),
    ]

    candidates = get_candidate_pool(mock_gh_client)

    assert {c.number for c in candidates} == {1, 2, 3}


def test_get_candidate_pool_excludes_triage_pending() -> None:
    """Test triage-pending issues are excluded from the pool."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [
        _issue(1, ["triage-pending"]),
        _issue(2, ["applied"]),
    ]

    candidates = get_candidate_pool(mock_gh_client)

    assert [c.number for c in candidates] == [2]


def test_get_candidate_pool_excludes_other_or_no_lifecycle_labels() -> None:
    """Test issues with unrelated or no lifecycle labels are excluded."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = [
        _issue(1, ["rejected"]),
        _issue(2, ["triage-mismatched"]),
        _issue(3, ["some-other-label"]),
        _issue(4, []),
        _issue(5, ["applied"]),
    ]

    candidates = get_candidate_pool(mock_gh_client)

    assert [c.number for c in candidates] == [5]


def test_get_candidate_pool_extracts_company_and_apply_url() -> None:
    """Test company/apply_url are extracted per candidate via parse_job_details."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    body = (
        "**Company:** Acme Corp\n"
        "**Role:** Staff Engineer\n"
        "**Apply URL:** https://acme.com/apply\n"
    )
    mock_gh_client.list_issues.return_value = [
        _issue(42, ["applied"], title="[Acme Corp] Staff Engineer", body=body)
    ]

    candidates = get_candidate_pool(mock_gh_client)

    assert candidates == [
        Candidate(
            number=42,
            title="[Acme Corp] Staff Engineer",
            company="Acme Corp",
            role="Staff Engineer",
            apply_url="https://acme.com/apply",
        )
    ]


def test_get_candidate_pool_returns_empty_list_when_no_open_issues() -> None:
    """Test an empty first page yields an empty candidate pool."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.list_issues.return_value = []

    candidates = get_candidate_pool(mock_gh_client)

    assert candidates == []
    mock_gh_client.list_issues.assert_called_once_with(
        state="open", per_page=PAGE_SIZE, page=1
    )


def test_get_candidate_pool_skips_issues_missing_a_number() -> None:
    """Test a malformed issue payload with no number is defensively skipped."""
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    malformed = _issue(1, ["applied"])
    del malformed["number"]
    mock_gh_client.list_issues.return_value = [
        malformed,
        _issue(2, ["applied"]),
    ]

    candidates = get_candidate_pool(mock_gh_client)

    assert [c.number for c in candidates] == [2]
