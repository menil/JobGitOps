"""Unit tests for the label-driven status transition coordinator."""

import os
from contextlib import contextmanager
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from jobgitops.cli.status_transition import LABEL_TO_STATUS, main
from jobgitops.schema import Settings

# Standard environment and CLI args used by most tests. centralizing these keeps
# the per-test setup focused on the single failure condition under test.
DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
DEFAULT_ARGV = ["status_transition.py", "--issue", "42", "--label", "applied"]


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
        patch("jobgitops.cli.status_transition.pathlib.Path.open"),
        patch(
            "jobgitops.cli.status_transition.json.load",
            return_value=event_data,
            side_effect=json_side_effect,
        ),
    ):
        yield


@pytest.fixture
def mock_settings() -> Settings:
    """Mock Settings object with Projects V2 configured."""
    return Settings(
        fit_threshold=4.0,
        projects_v2=mock.Mock(project_id="PVT_TEST_123", status_field_name="Status"),
    )


@pytest.fixture(autouse=True)
def mock_load_settings(mock_settings) -> MagicMock:
    """Patch load_settings to return Projects V2-configured settings by default.

    Individual tests override ``return_value`` / ``side_effect`` to exercise
    specific load or configuration branches.
    """
    with patch("jobgitops.cli.status_transition.load_settings") as mocked:
        mocked.return_value = mock_settings
        yield mocked


def test_label_to_status_mapping() -> None:
    """Verify the lifecycle label mapping covers every pipeline stage."""
    assert LABEL_TO_STATUS == {
        "triage-pending": "Triage Pending",
        "ready-to-apply": "Ready to Apply",
        "applied": "Applied",
        "in-loop": "In Loop",
        "offer-received": "Offer Received",
        "rejected": "Rejected",
        "triage-mismatched": "Mismatched/Closed",
    }


def test_transition_no_projects_v2_configured(mock_load_settings) -> None:
    """Verify that the script exits cleanly if Projects V2 is not configured."""
    mock_load_settings.return_value = Settings(fit_threshold=4.0, projects_v2=None)

    run_main(env={}, expected_code=0)


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_missing_token(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error if GITHUB_TOKEN is missing."""
    # No GITHUB_TOKEN: the client must never be reached.
    run_main(env={"GITHUB_REPOSITORY": "owner/repo"})

    assert "GITHUB_TOKEN environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


@pytest.mark.parametrize(
    ("label", "expected_status"),
    [
        ("triage-pending", "Triage Pending"),
        ("ready-to-apply", "Ready to Apply"),
        ("applied", "Applied"),
        ("in-loop", "In Loop"),
        ("offer-received", "Offer Received"),
        ("rejected", "Rejected"),
        ("triage-mismatched", "Mismatched/Closed"),
    ],
)
@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_from_cli_args(
    mock_github_client_class,
    label,
    expected_status,
) -> None:
    """Verify transition succeeds for each label passed via CLI args."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {"node_id": "ND_123"}
    mock_client.get_labels.return_value = []

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["status_transition.py", "--issue", "42", "--label", label]),
    ):
        main()

    # Client was initialized correctly
    mock_github_client_class.assert_called_once_with(
        token="test_token",
        repo="owner/repo",
        project_id="PVT_TEST_123",
        status_field_name="Status",
    )

    # get_issue called to resolve node ID
    mock_client.get_issue.assert_called_once_with(42)

    # update_project_status called with correct node ID and status
    mock_client.update_project_status.assert_called_once_with("ND_123", expected_status)


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_removes_stale_sibling_lifecycle_labels(
    mock_github_client_class,
) -> None:
    """The column owner keeps lifecycle labels exclusive: stale siblings dropped."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {"node_id": "ND_123"}
    mock_client.get_labels.return_value = ["applied", "ready-to-apply", "fit:A"]

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv", ["status_transition.py", "--issue", "42", "--label", "applied"]
        ),
    ):
        main()

    mock_client.remove_label.assert_called_once_with(42, "ready-to-apply")
    # The triggering label was already added by the webhook; never re-add it.
    mock_client.add_labels.assert_not_called()
    mock_client.update_project_status.assert_called_once_with("ND_123", "Applied")


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_from_event_payload(
    mock_github_client_class,
) -> None:
    """Verify transition succeeds using details from the webhook event payload."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "labeled",
        "label": {"name": "rejected"},
        "issue": {
            "number": 101,
            "node_id": "ND_EVENT_999",
            "labels": [{"name": "rejected"}],
        },
        "repository": {"full_name": "event_owner/event_repo"},
    }

    # GITHUB_REPOSITORY is missing in env, should load from event payload
    with (
        in_memory_event(event_data),
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch("sys.argv", ["status_transition.py", "--event-path", "event.json"]),
    ):
        main()

    # Initialized using repository from event payload
    mock_github_client_class.assert_called_once_with(
        token="test_token",
        repo="event_owner/event_repo",
        project_id="PVT_TEST_123",
        status_field_name="Status",
    )

    # get_issue should NOT be called since node_id was in payload
    mock_client.get_issue.assert_not_called()

    # update_project_status called with node ID and label-mapped status
    mock_client.update_project_status.assert_called_once_with(
        "ND_EVENT_999", "Rejected"
    )


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_from_closed_event_payload_default_rejected(
    mock_github_client_class,
) -> None:
    """Verify closed event maps to rejected status by default."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "closed",
        "issue": {
            "number": 101,
            "node_id": "ND_EVENT_999",
            "labels": [{"name": "triage-pending"}],
        },
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch("sys.argv", ["status_transition.py", "--event-path", "event.json"]),
    ):
        main()

    # Should update status to Rejected and sync rejected lifecycle label
    mock_client.update_project_status.assert_called_once_with(
        "ND_EVENT_999", "Rejected"
    )


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_from_closed_event_payload_triage_mismatched(
    mock_github_client_class,
) -> None:
    """Verify closed event maps to triage-mismatched if that label is present."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "closed",
        "issue": {
            "number": 101,
            "node_id": "ND_EVENT_999",
            "labels": [{"name": "triage-pending"}, {"name": "triage-mismatched"}],
        },
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch("sys.argv", ["status_transition.py", "--event-path", "event.json"]),
    ):
        main()

    # Should update status to Mismatched/Closed and sync
    # triage-mismatched lifecycle label.
    mock_client.update_project_status.assert_called_once_with(
        "ND_EVENT_999", "Mismatched/Closed"
    )


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_from_closed_event_payload_with_specific_mismatch_reason(
    mock_github_client_class,
) -> None:
    """Verify closed event maps to triage-mismatched if specific mismatch is present."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = ["location-mismatch"]

    event_data = {
        "action": "closed",
        "issue": {
            "number": 101,
            "node_id": "ND_EVENT_999",
            "labels": [{"name": "triage-pending"}, {"name": "location-mismatch"}],
        },
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch("sys.argv", ["status_transition.py", "--event-path", "event.json"]),
    ):
        main()

    # Should update status to Mismatched/Closed
    mock_client.update_project_status.assert_called_once_with(
        "ND_EVENT_999", "Mismatched/Closed"
    )


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_cli_label_overrides_event_payload(
    mock_github_client_class,
) -> None:
    """Verify --label wins over the event payload label when both are present."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "labeled",
        "label": {"name": "rejected"},
        "issue": {"number": 101, "node_id": "ND_EVENT_999"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            [
                "status_transition.py",
                "--event-path",
                "event.json",
                "--label",
                "in-loop",
            ],
        ),
    ):
        main()

    mock_client.update_project_status.assert_called_once_with("ND_EVENT_999", "In Loop")


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_cli_label_fills_missing_payload_label(
    mock_github_client_class,
) -> None:
    """Verify --label is used when the event payload carries no label."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "labeled",
        "issue": {"number": 101, "node_id": "ND_EVENT_999"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            [
                "status_transition.py",
                "--event-path",
                "event.json",
                "--label",
                "in-loop",
            ],
        ),
    ):
        main()

    mock_client.update_project_status.assert_called_once_with("ND_EVENT_999", "In Loop")


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_cli_issue_and_label_override_payload(
    mock_github_client_class,
) -> None:
    """Verify --issue/--label win while the payload node_id is reused."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "labeled",
        "label": {"name": "rejected"},
        "issue": {"number": "101", "node_id": "ND_EVENT_999"},
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            [
                "status_transition.py",
                "--event-path",
                "event.json",
                "--issue",
                "42",
                "--label",
                "in-loop",
            ],
        ),
    ):
        main()

    # Issue #42 from CLI and payload node_id: no API fetch needed.
    mock_client.get_issue.assert_not_called()
    mock_client.update_project_status.assert_called_once_with("ND_EVENT_999", "In Loop")


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_payload_number_coerced_to_int(
    mock_github_client_class,
) -> None:
    """Verify a string issue number from the payload is coerced to an int."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_labels.return_value = []

    event_data = {
        "action": "labeled",
        "label": {"name": "applied"},
        "issue": {"number": "101", "node_id": "ND_EVENT_999"},
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with (
        in_memory_event(event_data),
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch("sys.argv", ["status_transition.py", "--event-path", "event.json"]),
    ):
        main()

    mock_client.get_issue.assert_not_called()
    mock_client.update_project_status.assert_called_once_with("ND_EVENT_999", "Applied")


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_invalid_payload_number(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify a non-numeric issue number from the payload exits with error."""
    event_data = {
        "action": "labeled",
        "label": {"name": "applied"},
        "issue": {"number": "not-a-number", "node_id": "ND_EVENT_999"},
        "repository": {"full_name": "event_owner/event_repo"},
    }

    with in_memory_event(event_data):
        run_main(
            env={"GITHUB_TOKEN": "test_token"},
            argv=["status_transition.py", "--event-path", "event.json"],
        )

    assert "No issue number or event payload specified." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_missing_label(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when no label is provided."""
    run_main(argv=["status_transition.py", "--issue", "42"])

    assert "Unsupported or missing label" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_unknown_label(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error for a label outside the known set."""
    run_main(argv=["status_transition.py", "--issue", "42", "--label", "unknown-label"])

    assert "Unsupported or missing label 'unknown-label'" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_settings_load_failure(
    mock_github_client_class,
    mock_load_settings,
    caplog,
) -> None:
    """Verify script exits with error when settings cannot be loaded."""
    mock_load_settings.side_effect = RuntimeError("bad settings")

    run_main(env={})

    assert "Failed to load settings configuration: bad settings" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_invalid_event_payload(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when the event payload is not valid JSON."""
    with in_memory_event(json_side_effect=ValueError("bad json")):
        run_main(argv=["status_transition.py", "--event-path", "event.json"])

    assert "Failed to parse event payload JSON:" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_invalid_event_payload_from_env(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits when GITHUB_EVENT_PATH env var points to invalid JSON."""
    # No --event-path arg: the script must pick up GITHUB_EVENT_PATH from the env.
    with in_memory_event(json_side_effect=ValueError("bad json")):
        run_main(env={**DEFAULT_ENV, "GITHUB_EVENT_PATH": "event.json"})

    assert "Failed to parse event payload JSON:" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_missing_repository(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when GITHUB_REPOSITORY is missing."""
    # No GITHUB_REPOSITORY: the client must never be reached.
    run_main(env={"GITHUB_TOKEN": "test_token"})

    assert "GITHUB_REPOSITORY environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_missing_issue_number(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when no issue number can be resolved."""
    # No --issue and no event payload: no issue number to act on.
    run_main(argv=["status_transition.py", "--label", "applied"])

    assert "No issue number or event payload specified." in caplog.text
    mock_github_client_class.assert_not_called()


def test_transition_client_init_failure(caplog) -> None:
    """Verify script exits with error when GitHubClient cannot be initialized."""
    # Real client (unmocked): invalid repo format triggers ValueError in __init__
    # before any network I/O, matching the guard production actually relies on.
    run_main(
        env={"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "not-a-valid-repo"}
    )

    assert "Failed to initialize GitHubClient: Invalid repository format" in caplog.text


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_get_issue_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when fetching the issue fails."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.side_effect = RuntimeError("api down")

    run_main()

    assert "Failed to fetch issue #42: api down" in caplog.text
    mock_client.update_project_status.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_missing_node_id(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when the issue node ID cannot be resolved."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {}  # No node_id

    run_main()

    assert "Failed to resolve issue node ID for #42." in caplog.text
    mock_client.update_project_status.assert_not_called()


@patch("jobgitops.cli.status_transition.GitHubClient")
def test_transition_update_status_failure(
    mock_github_client_class,
    caplog,
) -> None:
    """Verify script exits with error when updating the project status fails."""
    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {"node_id": "ND_123"}
    mock_client.get_labels.return_value = []
    mock_client.update_project_status.side_effect = RuntimeError("graphql failed")

    run_main()

    assert "Failed to update Projects V2 status: graphql failed" in caplog.text
