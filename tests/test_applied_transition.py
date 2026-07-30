"""Unit tests for the applied status transition coordinator (applied_transition.py)."""

import json
import os
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from applied_transition import main
from jobgitops.schema import Settings


@pytest.fixture
def mock_settings() -> Settings:
    """Mock Settings object with Projects V2 configured."""
    return Settings(
        fit_threshold=4.0,
        projects_v2=mock.Mock(project_id="PVT_TEST_123", status_field_name="Status"),
    )


@patch("applied_transition.load_settings")
def test_transition_no_projects_v2_configured(mock_load_settings) -> None:
    """Verify that the script exits cleanly if Projects V2 is not configured."""
    # Settings with projects_v2 set to None
    mock_load_settings.return_value = Settings(fit_threshold=4.0, projects_v2=None)

    with patch("sys.argv", ["applied_transition.py"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


@patch("applied_transition.load_settings")
def test_transition_missing_env_vars(mock_load_settings, mock_settings) -> None:
    """Verify script exits with error if GITHUB_TOKEN is missing."""
    mock_load_settings.return_value = mock_settings

    environ_mock = {"GITHUB_REPOSITORY": "owner/repo"}  # No GITHUB_TOKEN
    with (
        patch.dict(os.environ, environ_mock, clear=True),
        patch("sys.argv", ["applied_transition.py", "--issue", "42"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_from_cli_args(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
) -> None:
    """Verify transition succeeds when issue is passed via CLI args."""
    mock_load_settings.return_value = mock_settings

    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {"node_id": "ND_123"}

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with (
        patch.dict(os.environ, environ_mock, clear=True),
        patch("sys.argv", ["applied_transition.py", "--issue", "42"]),
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

    # update_project_status called with correct node ID
    mock_client.update_project_status.assert_called_once_with("ND_123", "Applied")


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_from_event_payload(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    tmp_path,
) -> None:
    """Verify transition succeeds using details from event payload JSON file."""
    mock_load_settings.return_value = mock_settings

    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client

    # Create temporary event payload JSON
    event_data = {
        "action": "labeled",
        "issue": {
            "number": 101,
            "node_id": "ND_EVENT_999",
            "labels": [{"name": "applied"}],
        },
        "repository": {"full_name": "event_owner/event_repo"},
    }
    payload_file = tmp_path / "event.json"
    with payload_file.open("w", encoding="utf-8") as f:
        json.dump(event_data, f)

    # GITHUB_REPOSITORY is missing in env, should load from event payload
    environ_mock = {"GITHUB_TOKEN": "test_token"}
    with (
        patch.dict(os.environ, environ_mock, clear=True),
        patch(
            "sys.argv",
            ["applied_transition.py", "--event-path", str(payload_file)],
        ),
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

    # update_project_status called with node ID from payload
    mock_client.update_project_status.assert_called_once_with("ND_EVENT_999", "Applied")
