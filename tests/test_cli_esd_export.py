"""Unit tests for the ESD export CLI entry point (jobgitops.cli.esd_export)."""

import datetime as dt
import os
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from jobgitops.cli.esd_export import main
from jobgitops.github_client import GitHubClientError

DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
DEFAULT_ARGV = [
    "esd_export.py",
    "--group-by",
    "weekly",
    "--format",
    "csv",
    "--output",
    "out.csv",
]


def run_main(
    env: dict[str, str] | None = None,
    argv: list[str] | None = None,
    expected_code: int | None = None,
) -> int | None:
    """Run main() under patched env/argv, asserting the exit code if given."""
    with (
        patch.dict(os.environ, DEFAULT_ENV if env is None else env, clear=True),
        patch("sys.argv", DEFAULT_ARGV if argv is None else argv),
    ):
        if expected_code is None:
            main()
            return None
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == expected_code
        return exc_info.value.code


@patch("jobgitops.cli.esd_export.write_rows")
@patch("jobgitops.cli.esd_export.export_rows")
@patch("jobgitops.cli.esd_export.GitHubClient")
def test_missing_token_exits_before_client_construction(
    mock_client_class: MagicMock,
    mock_export_rows: MagicMock,
    mock_write_rows: MagicMock,
    caplog,
) -> None:
    run_main(env={"GITHUB_REPOSITORY": "owner/repo"}, expected_code=1)
    assert "GITHUB_TOKEN environment variable is missing." in caplog.text
    mock_client_class.assert_not_called()
    mock_export_rows.assert_not_called()
    mock_write_rows.assert_not_called()


@patch("jobgitops.cli.esd_export.GitHubClient")
def test_missing_repository_exits_before_client_construction(
    mock_client_class: MagicMock, caplog
) -> None:
    run_main(env={"GITHUB_TOKEN": "test_token"}, expected_code=1)
    assert "GITHUB_REPOSITORY environment variable is missing." in caplog.text
    mock_client_class.assert_not_called()


@patch("jobgitops.cli.esd_export.write_rows")
@patch("jobgitops.cli.esd_export.export_rows")
@patch("jobgitops.cli.esd_export.GitHubClient")
def test_happy_path_wires_args_through_to_pipeline_and_writer(
    mock_client_class: MagicMock,
    mock_export_rows: MagicMock,
    mock_write_rows: MagicMock,
    caplog,
) -> None:
    caplog.set_level("INFO")
    mock_client = mock.Mock()
    mock_client_class.return_value = mock_client
    mock_export_rows.return_value = [{"date": "2026-01-05"}, {"date": "2026-01-06"}]

    argv = [
        "esd_export.py",
        "--group-by",
        "weekly",
        "--week-start",
        "wednesday",
        "--format",
        "xlsx",
        "--max-per-period",
        "3",
        "--start-date",
        "2026-01-01",
        "--end-date",
        "2026-01-31",
        "--output",
        "/tmp/out.xlsx",
    ]
    run_main(argv=argv)

    mock_client_class.assert_called_once_with(token="test_token", repo="owner/repo")
    mock_export_rows.assert_called_once_with(
        mock_client,
        group_by="weekly",
        week_start="wednesday",
        max_per_period=3,
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 1, 31),
    )
    mock_write_rows.assert_called_once_with(
        mock_export_rows.return_value, "/tmp/out.xlsx", "xlsx"
    )
    assert "Wrote 2 row(s) to /tmp/out.xlsx" in caplog.text


@patch("jobgitops.cli.esd_export.write_rows")
@patch("jobgitops.cli.esd_export.export_rows")
@patch("jobgitops.cli.esd_export.GitHubClient")
def test_defaults_are_week_start_monday_and_unlimited_cap(
    mock_client_class: MagicMock,
    mock_export_rows: MagicMock,
    mock_write_rows: MagicMock,
) -> None:
    mock_export_rows.return_value = []
    run_main()  # DEFAULT_ARGV: only --group-by/--format/--output given
    mock_export_rows.assert_called_once_with(
        mock_client_class.return_value,
        group_by="weekly",
        week_start="monday",
        max_per_period=None,
        start_date=None,
        end_date=None,
    )


@patch("jobgitops.cli.esd_export.GitHubClient")
def test_export_rows_value_error_exits_with_message(
    mock_client_class: MagicMock, caplog
) -> None:
    with patch(
        "jobgitops.cli.esd_export.export_rows", side_effect=ValueError("bad input")
    ):
        run_main(expected_code=1)
    assert "Failed to build ESD export: bad input" in caplog.text


@patch("jobgitops.cli.esd_export.GitHubClient")
def test_export_rows_github_client_error_exits_with_message(
    mock_client_class: MagicMock, caplog
) -> None:
    with patch(
        "jobgitops.cli.esd_export.export_rows",
        side_effect=GitHubClientError("API down"),
    ):
        run_main(expected_code=1)
    assert "Failed to build ESD export: API down" in caplog.text


@patch("jobgitops.cli.esd_export.export_rows", return_value=[])
@patch("jobgitops.cli.esd_export.GitHubClient")
def test_write_rows_failure_exits_with_message(
    mock_client_class: MagicMock, mock_export_rows: MagicMock, caplog
) -> None:
    with patch(
        "jobgitops.cli.esd_export.write_rows",
        side_effect=OSError("disk full"),
    ):
        run_main(expected_code=1)
    assert "Failed to write out.csv: disk full" in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        # --group-by is required.
        ["esd_export.py", "--format", "csv", "--output", "out.csv"],
        # --format is required.
        ["esd_export.py", "--group-by", "weekly", "--output", "out.csv"],
        # --output is required.
        ["esd_export.py", "--group-by", "weekly", "--format", "csv"],
        # --group-by must be one of the two enum values.
        [
            "esd_export.py",
            "--group-by",
            "daily",
            "--format",
            "csv",
            "--output",
            "out.csv",
        ],
        # --week-start must be a real weekday name.
        [
            "esd_export.py",
            "--group-by",
            "weekly",
            "--week-start",
            "someday",
            "--format",
            "csv",
            "--output",
            "out.csv",
        ],
        # --format must be csv or xlsx.
        [
            "esd_export.py",
            "--group-by",
            "weekly",
            "--format",
            "pdf",
            "--output",
            "out.csv",
        ],
    ],
)
def test_argparse_rejects_missing_or_invalid_enum_args(argv: list[str]) -> None:
    run_main(argv=argv, expected_code=2)


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_max_per_period_rejects_non_positive_and_non_numeric(value: str) -> None:
    argv = [
        "esd_export.py",
        "--group-by",
        "weekly",
        "--format",
        "csv",
        "--output",
        "out.csv",
        "--max-per-period",
        value,
    ]
    run_main(argv=argv, expected_code=2)


def test_max_per_period_unlimited_is_case_insensitive() -> None:
    """'Unlimited'/'UNLIMITED' parse the same as 'unlimited' -> no cap."""
    argv = [
        "esd_export.py",
        "--group-by",
        "weekly",
        "--format",
        "csv",
        "--output",
        "out.csv",
        "--max-per-period",
        "UNLIMITED",
    ]
    with (
        patch("jobgitops.cli.esd_export.GitHubClient"),
        patch("jobgitops.cli.esd_export.write_rows"),
        patch("jobgitops.cli.esd_export.export_rows", return_value=[]) as mock_export,
    ):
        run_main(argv=argv)
    assert mock_export.call_args.kwargs["max_per_period"] is None


@pytest.mark.parametrize("flag", ["--start-date", "--end-date"])
def test_date_flags_reject_non_iso_format(flag: str) -> None:
    argv = [
        "esd_export.py",
        "--group-by",
        "weekly",
        "--format",
        "csv",
        "--output",
        "out.csv",
        flag,
        "01/05/2026",
    ]
    run_main(argv=argv, expected_code=2)


def test_start_date_after_end_date_rejected() -> None:
    argv = [
        "esd_export.py",
        "--group-by",
        "weekly",
        "--format",
        "csv",
        "--output",
        "out.csv",
        "--start-date",
        "2026-02-01",
        "--end-date",
        "2026-01-01",
    ]
    run_main(argv=argv, expected_code=2)
