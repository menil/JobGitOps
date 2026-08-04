"""Unit tests for the two-way project-label sync coordinator."""

import os
from contextlib import contextmanager
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from jobgitops.schema import Settings
from project_sync import main

# Standard environment used by most tests; per-test overrides focus on the
# single failure condition under test.
DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
DEFAULT_ARGV = ["project_sync.py", "event", "--event-path", "event.json"]


def run_main(
    env: dict[str, str] | None = None,
    argv: list[str] | None = None,
    expected_code: int = 1,
) -> None:
    """Run main() under patched env/argv and assert the process exit code."""
    with (
        patch.dict(os.environ, DEFAULT_ENV if env is None else env, clear=True),
        patch("sys.argv", DEFAULT_ARGV if argv is None else argv),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == expected_code


@contextmanager
def in_memory_event(event_data=None, json_side_effect=None):
    """Serve a webhook payload in memory instead of writing a JSON file."""
    with (
        patch("project_sync.pathlib.Path.open"),
        patch(
            "project_sync.json.load",
            return_value=event_data,
            side_effect=json_side_effect,
        ),
    ):
        yield


def project_item_event(to_status, **overrides):
    """Build a realistic projects_v2_item edited-event payload."""
    item = {
        "project_node_id": "PVT_TEST_123",
        "content_type": "Issue",
        "content_node_id": "ND_EVENT_999",
        "field_value_by_name": {
            "Status": {"type": "single_select", "name": "Status", "value": to_status}
        },
    }
    item.update(overrides)
    return {
        "action": "edited",
        "projects_v2_item": item,
        "changes": {
            "field_value": {
                "field_node_id": "FIELD_1",
                "field_name": "Status",
                "from": "In progress",
                "to": to_status,
            }
        },
    }


@pytest.fixture
def mock_settings() -> Settings:
    """Mock Settings object with Projects V2 configured."""
    return Settings(
        fit_threshold=4.0,
        projects_v2=mock.Mock(project_id="PVT_TEST_123", status_field_name="Status"),
    )


@pytest.fixture(autouse=True)
def mock_load_settings(mock_settings) -> MagicMock:
    """Patch load_settings to return Projects V2-configured settings by default."""
    with patch("project_sync.load_settings") as mocked:
        mocked.return_value = mock_settings
        yield mocked


def test_sync_no_projects_v2_configured(mock_load_settings) -> None:
    """Verify that the script exits cleanly if Projects V2 is not configured."""
    mock_load_settings.return_value = Settings(fit_threshold=4.0, projects_v2=None)

    run_main(env={}, expected_code=0)


@patch("project_sync.GitHubClient")
def test_sync_missing_token(mock_github_client_class, caplog) -> None:
    """Verify script exits with error if GITHUB_TOKEN is missing."""
    run_main(env={"GITHUB_REPOSITORY": "owner/repo"})

    assert "GITHUB_TOKEN environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_missing_repository(mock_github_client_class, caplog) -> None:
    """Verify script exits with error if GITHUB_REPOSITORY is missing."""
    run_main(env={"GITHUB_TOKEN": "test_token"})

    assert "GITHUB_REPOSITORY environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_path_rejected_for_backfill(mock_github_client_class) -> None:
    """Verify --event-path is rejected for commands that do not read events."""
    run_main(
        argv=["project_sync.py", "backfill", "--event-path", "event.json"],
        expected_code=2,
    )


@patch("project_sync.GitHubClient")
def test_sync_event_missing_payload(mock_github_client_class, caplog) -> None:
    """Verify script exits when no event payload is provided."""
    run_main(argv=["project_sync.py", "event"])

    assert "No event path specified for the 'event' command." in caplog.text


@patch("project_sync.GitHubClient")
def test_sync_event_applies_reverse_label(
    mock_github_client_class,
) -> None:
    """Verify a column move to Applied adds the 'applied' lifecycle label."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.return_value = ["fit:A"]
    mock_client.project_id = "PVT_TEST_123"

    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_github_client_class.assert_called_once_with(
        token="test_token",
        repo="owner/repo",
        project_id="PVT_TEST_123",
        status_field_name="Status",
    )
    mock_client.resolve_issue_number.assert_called_once_with("ND_EVENT_999")
    mock_client.add_labels.assert_called_once_with(42, ["applied"])


@patch("project_sync.GitHubClient")
def test_sync_event_cleans_up_stale_sibling_label(
    mock_github_client_class,
) -> None:
    """Verify the target label plus a stale sibling still triggers cleanup.

    The sibling (here 'in-loop') must be removed even though the target label
    is already present, otherwise a dropped or raced label edit leaves drift.
    """
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.return_value = ["applied", "in-loop"]
    mock_client.project_id = "PVT_TEST_123"

    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.remove_label.assert_called_once_with(42, "in-loop")
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_missing_project_node(
    mock_github_client_class,
) -> None:
    """Verify an event without a project_node_id is treated as foreign."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    with (
        in_memory_event(project_item_event("Applied", project_node_id=None)),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_non_string_status(
    mock_github_client_class,
) -> None:
    """Verify a malformed (non-string) status value is skipped safely."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.project_id = "PVT_TEST_123"
    event = project_item_event("Applied")
    event["changes"]["field_value"]["to"] = 123

    with (
        in_memory_event(event),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_noop_when_label_present(
    mock_github_client_class,
) -> None:
    """Verify the reverse sync no-ops when the target label is already present."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.return_value = ["applied"]
    mock_client.project_id = "PVT_TEST_123"

    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_called_once_with("ND_EVENT_999")
    mock_client.add_labels.assert_not_called()
    mock_client.remove_label.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_triage_pending(
    mock_github_client_class,
) -> None:
    """Verify dragging a card back to Triage Pending never re-adds the label."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    mock_client.project_id = "PVT_TEST_123"
    with (
        in_memory_event(project_item_event("Triage Pending")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_non_issue_item(
    mock_github_client_class,
) -> None:
    """Verify events for pull requests and draft issues are skipped."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    with (
        in_memory_event(project_item_event("Applied", content_type="PullRequest")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_other_project(
    mock_github_client_class,
) -> None:
    """Verify events for a different project board are skipped."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    with (
        in_memory_event(project_item_event("Applied", project_node_id="PVT_OTHER_1")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_ignores_other_field_edit(
    mock_github_client_class,
) -> None:
    """Verify edits to a non-status field do not trigger a reverse sync."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    event = project_item_event("Applied")
    mock_client.project_id = "PVT_TEST_123"
    event["changes"]["field_value"]["field_name"] = "Priority"

    with (
        in_memory_event(event),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_created_reads_current_value(
    mock_github_client_class,
) -> None:
    """Verify created events (no delta) read the current Status value."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.return_value = []

    mock_client.project_id = "PVT_TEST_123"
    event = project_item_event("In Loop")
    event["action"] = "created"
    del event["changes"]

    with (
        in_memory_event(event),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.add_labels.assert_called_once_with(42, ["in-loop"])


@patch("project_sync.GitHubClient")
def test_sync_event_created_ignores_unknown_status(
    mock_github_client_class,
) -> None:
    """Verify created events with an unknown/unmapped status are skipped."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    mock_client.project_id = "PVT_TEST_123"
    event = project_item_event("In progress")
    event["action"] = "created"
    del event["changes"]

    with (
        in_memory_event(event),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
    ):
        main()

    mock_client.resolve_issue_number.assert_not_called()
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_resolve_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when the issue node cannot be resolved."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.side_effect = RuntimeError("graphql down")

    mock_client.project_id = "PVT_TEST_123"
    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "Failed to resolve issue for node ND_EVENT_999: graphql down" in caplog.text


@patch("project_sync.GitHubClient")
def test_sync_event_missing_content_node(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits when the payload lacks content_node_id."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    mock_client.project_id = "PVT_TEST_123"
    with (
        in_memory_event(project_item_event("Applied", content_node_id=None)),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "missing content_node_id" in caplog.text


@patch("project_sync.GitHubClient")
def test_sync_backfill_moves_issues(
    mock_github_client_class,
) -> None:
    """Verify backfill maps labels to columns and fills unlabeled issues."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            {"number": 1, "node_id": "ND_1", "state": "open", "labels": []},
            {
                "number": 2,
                "node_id": "ND_2",
                "state": "closed",
                "labels": [],
            },
            {
                "number": 3,
                "node_id": "ND_3",
                "state": "open",
                "labels": [{"name": "applied"}],
            },
        ],
        [],
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill"]),
    ):
        main()

    mock_client.list_issues.assert_called_with(state="all", per_page=100, page=1)
    mock_client.update_project_status.assert_has_calls(
        [
            mock.call("ND_1", "Triage Pending"),
            mock.call("ND_2", "Rejected"),
            mock.call("ND_3", "Applied"),
        ]
    )


@patch("project_sync.GitHubClient")
def test_sync_backfill_continues_on_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify a failed move is logged while the rest of the backfill proceeds."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            {"number": 1, "node_id": "ND_1", "state": "open", "labels": []},
            {
                "number": 2,
                "node_id": "ND_2",
                "state": "open",
                "labels": [{"name": "rejected"}],
            },
        ],
        [],
    ]
    mock_client.update_project_status.side_effect = RuntimeError("api down")

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert mock_client.update_project_status.call_count == 2
    assert "Backfill failed for issue #1: api down" in caplog.text


@patch("project_sync.GitHubClient")
def test_sync_backfill_closed_with_lifecycle_label_wins(
    mock_github_client_class,
) -> None:
    """Verify a lifecycle label wins over the closed-state default."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            {
                "number": 5,
                "node_id": "ND_5",
                "state": "closed",
                "labels": [{"name": "triage-mismatched"}],
            }
        ],
        [],
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill"]),
    ):
        main()

    mock_client.update_project_status.assert_called_once_with(
        "ND_5", "Mismatched/Closed"
    )


@patch("project_sync.GitHubClient")
def test_sync_backfill_skips_already_correct_columns(
    mock_github_client_class,
) -> None:
    """Verify backfill does not re-move cards already in the right column."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            {"number": 1, "node_id": "ND_1", "state": "open", "labels": []},
            {
                "number": 2,
                "node_id": "ND_2",
                "state": "open",
                "labels": [{"name": "applied"}],
            },
        ],
        [],
    ]
    mock_client.list_project_items.return_value = {1: "Triage Pending", 2: "Applied"}

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill"]),
    ):
        main()

    mock_client.update_project_status.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_backfill_reverse_reconciles_labels(
    mock_github_client_class,
) -> None:
    """Verify --reverse applies the label matching a card's current column."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            # Card sits on Applied but the issue still says in-loop (dropped
            # webhook event); reverse sync must switch it to applied.
            {
                "number": 1,
                "node_id": "ND_1",
                "state": "open",
                "labels": [{"name": "in-loop"}],
            },
        ],
        [],
    ]
    mock_client.list_project_items.return_value = {1: "Applied"}

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill", "--reverse"]),
    ):
        main()

    mock_client.add_labels.assert_called_once_with(1, ["applied"])
    # The reconciled label now matches the board, so the forward pass skips.
    mock_client.update_project_status.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_backfill_reverse_skips_aligned_and_excluded(
    mock_github_client_class,
) -> None:
    """Verify --reverse skips already-aligned cards and Triage Pending."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.list_issues.side_effect = [
        [
            {
                "number": 1,
                "node_id": "ND_1",
                "state": "open",
                "labels": [{"name": "applied"}],
            },
            {
                "number": 2,
                "node_id": "ND_2",
                "state": "open",
                "labels": [],
            },
        ],
        [],
    ]
    mock_client.list_project_items.return_value = {1: "Applied", 2: "Triage Pending"}

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill", "--reverse"]),
    ):
        main()

    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_backfill_paginates_full_pages(
    mock_github_client_class,
) -> None:
    """Verify backfill keeps fetching while pages come back full."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    full_page = [
        {"number": i, "node_id": f"ND_{i}", "state": "open", "labels": []}
        for i in range(1, 101)
    ]
    mock_client.list_issues.side_effect = [
        full_page,
        [{"number": 101, "node_id": "ND_101", "state": "open", "labels": []}],
    ]
    mock_client.list_project_items.return_value = {}

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "backfill"]),
    ):
        main()

    assert mock_client.list_issues.call_count == 2
    assert mock_client.update_project_status.call_count == 101


@patch("project_sync.GitHubClient")
def test_sync_field_options_adds_missing_only(
    mock_github_client_class,
) -> None:
    """Verify field-options keeps existing options and appends missing ones."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_status_field_options.return_value = [
        "Backlog",
        "In progress",
        "Done",
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "field-options"]),
    ):
        main()

    mock_client.update_status_field_options.assert_called_once_with(
        [
            "Backlog",
            "In progress",
            "Done",
            "Triage Pending",
            "Ready to Apply",
            "Applied",
            "In Loop",
            "Offer Received",
            "Rejected",
            "Mismatched/Closed",
        ]
    )


@patch("project_sync.GitHubClient")
def test_sync_field_options_adds_missing_noop_when_complete(
    mock_github_client_class,
) -> None:
    """Verify field-options does nothing when all lifecycle options exist."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_status_field_options.return_value = [
        "Triage Pending",
        "Ready to Apply",
        "Applied",
        "In Loop",
        "Offer Received",
        "Rejected",
        "Mismatched/Closed",
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "field-options"]),
    ):
        main()

    mock_client.update_status_field_options.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_field_options_prunes_stale(
    mock_github_client_class,
) -> None:
    """Verify --prune replaces the option set with exactly the lifecycle model."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_status_field_options.return_value = [
        "Backlog",
        "In progress",
        "Done",
        "Triage Pending",
        "Applied",
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "field-options", "--prune"]),
    ):
        main()

    mock_client.update_status_field_options.assert_called_once_with(
        [
            "Triage Pending",
            "Ready to Apply",
            "Applied",
            "In Loop",
            "Offer Received",
            "Rejected",
            "Mismatched/Closed",
        ]
    )


@patch("project_sync.GitHubClient")
def test_sync_field_options_prune_noop_when_aligned(
    mock_github_client_class,
) -> None:
    """Verify --prune does nothing when no stale options remain."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_status_field_options.return_value = [
        "Triage Pending",
        "Ready to Apply",
        "Applied",
        "In Loop",
        "Offer Received",
        "Rejected",
        "Mismatched/Closed",
    ]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "field-options", "--prune"]),
    ):
        main()

    mock_client.update_status_field_options.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_label_read_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify a failed label read exits instead of syncing blind."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.side_effect = RuntimeError("labels api down")
    mock_client.project_id = "PVT_TEST_123"

    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "Failed to read labels for issue #42: labels api down" in caplog.text
    mock_client.add_labels.assert_not_called()


@patch("project_sync.GitHubClient")
def test_sync_event_label_apply_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify a failed label apply exits with an error."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.resolve_issue_number.return_value = 42
    mock_client.get_labels.return_value = []
    mock_client.add_labels.side_effect = RuntimeError("labels api down")
    mock_client.project_id = "PVT_TEST_123"

    with (
        in_memory_event(project_item_event("Applied")),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "Failed to apply label 'applied' to issue #42" in caplog.text


@patch("project_sync.GitHubClient")
def test_sync_field_options_error_exits(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify a field-options failure exits non-zero instead of passing silently."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_status_field_options.side_effect = RuntimeError("graphql down")

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["project_sync.py", "field-options"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "Failed to sync status field options: graphql down" in caplog.text
