"""Unit tests for the applied status transition coordinator (applied_transition.py)."""

import json
import os
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from applied_transition import main
from jobgitops.schema import Settings

# Standard environment and CLI args used by most tests. centralizing these keeps
# the per-test setup focused on the single failure condition under test.
DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
DEFAULT_ARGV = ["applied_transition.py", "--issue", "42"]


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
    mock_load_settings.return_value = Settings(fit_threshold=4.0, projects_v2=None)

    run_main(env={}, expected_code=0)


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_missing_token(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error if GITHUB_TOKEN is missing."""
    mock_load_settings.return_value = mock_settings

    # No GITHUB_TOKEN: the client must never be reached.
    run_main(env={"GITHUB_REPOSITORY": "owner/repo"})

    assert "GITHUB_TOKEN environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


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

    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", DEFAULT_ARGV),
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
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
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


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
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


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_invalid_event_payload(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    tmp_path,
    caplog,
) -> None:
    """Verify script exits with error when the CLI event payload is not valid JSON."""
    mock_load_settings.return_value = mock_settings

    bad_payload = tmp_path / "event.json"
    bad_payload.write_text("{not valid json", encoding="utf-8")

    run_main(argv=["applied_transition.py", "--event-path", str(bad_payload)])

    assert "Failed to parse event payload JSON:" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_invalid_event_payload_from_env(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    tmp_path,
    caplog,
) -> None:
    """Verify script exits when GITHUB_EVENT_PATH env var points to invalid JSON."""
    mock_load_settings.return_value = mock_settings

    bad_payload = tmp_path / "event.json"
    bad_payload.write_text("{not valid json", encoding="utf-8")

    # No --event-path arg: the script must pick up GITHUB_EVENT_PATH from the env.
    run_main(env={**DEFAULT_ENV, "GITHUB_EVENT_PATH": str(bad_payload)})

    assert "Failed to parse event payload JSON:" in caplog.text
    mock_github_client_class.assert_not_called()


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_missing_repository(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error when GITHUB_REPOSITORY is missing."""
    mock_load_settings.return_value = mock_settings

    # No GITHUB_REPOSITORY: the client must never be reached.
    run_main(env={"GITHUB_TOKEN": "test_token"})

    assert "GITHUB_REPOSITORY environment variable is missing." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_missing_issue_number(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error when no issue number can be resolved."""
    mock_load_settings.return_value = mock_settings

    # No --issue and no event payload: no issue number to act on.
    run_main(argv=["applied_transition.py"])

    assert "No issue number or event payload specified." in caplog.text
    mock_github_client_class.assert_not_called()


@patch("applied_transition.load_settings")
def test_transition_client_init_failure(
    mock_load_settings, mock_settings, caplog
) -> None:
    """Verify script exits with error when GitHubClient cannot be initialized."""
    mock_load_settings.return_value = mock_settings

    # Real client (unmocked): invalid repo format triggers ValueError in __init__
    # before any network I/O, matching the guard production actually relies on.
    run_main(
        env={"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "not-a-valid-repo"}
    )

    assert "Failed to initialize GitHubClient: Invalid repository format" in caplog.text


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_get_issue_failure(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error when fetching the issue fails."""
    mock_load_settings.return_value = mock_settings

    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.side_effect = RuntimeError("api down")

    run_main()

    assert "Failed to fetch issue #42: api down" in caplog.text
    mock_client.update_project_status.assert_not_called()


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_missing_node_id(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error when the issue node ID cannot be resolved."""
    mock_load_settings.return_value = mock_settings

    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {}  # No node_id

    run_main()

    assert "Failed to resolve issue node ID for #42." in caplog.text
    mock_client.update_project_status.assert_not_called()


@patch("applied_transition.load_settings")
@patch("applied_transition.GitHubClient")
def test_transition_update_status_failure(
    mock_github_client_class,
    mock_load_settings,
    mock_settings,
    caplog,
) -> None:
    """Verify script exits with error when updating the project status fails."""
    mock_load_settings.return_value = mock_settings

    mock_client = MagicMock()
    mock_github_client_class.return_value = mock_client
    mock_client.get_issue.return_value = {"node_id": "ND_123"}
    mock_client.update_project_status.side_effect = RuntimeError("graphql failed")

    run_main()

    assert "Failed to update Projects V2 status: graphql failed" in caplog.text
