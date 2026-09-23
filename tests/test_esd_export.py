"""Unit tests for the ESD export pipeline."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pandas as pd
import pytest

from jobgitops.esd_export import _ActivityEvent, _build_row, export_rows, write_rows


def _issue(
    number: int, company: str, role: str, source: str = "", url: str = ""
) -> dict:
    body_lines = [
        "## Job Description",
        "Body text.",
        "",
        f"**Company:** {company}",
        f"**Role:** {role}",
    ]
    if source:
        body_lines.append(f"**Source:** {source}")
    if url:
        body_lines.append(f"**Apply URL:** {url}")
    return {
        "number": number,
        "title": f"[{company}] {role}",
        "body": "\n".join(body_lines),
    }


def _labeled_event(label: str, created_at: str) -> dict:
    return {"event": "labeled", "label": label, "created_at": created_at}


def _unlabeled_event(label: str, created_at: str) -> dict:
    return {"event": "unlabeled", "label": label, "created_at": created_at}


def _make_client(
    issues: list[dict], events_by_issue: dict[int, list[dict]]
) -> MagicMock:
    """Build a fake GitHubClient: one page of issues, per-issue timelines."""
    client = MagicMock()
    client.list_issues.side_effect = [issues, []]
    client.list_issue_label_events.side_effect = lambda issue_number: (
        events_by_issue.get(issue_number, [])
    )
    return client


def test_filters_to_activity_labels_only() -> None:
    """rejected/offer-received/triage-pending events contribute no rows."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            _labeled_event("triage-pending", "2026-01-01T00:00:00Z"),
            _labeled_event("rejected", "2026-01-10T00:00:00Z"),
            _labeled_event("offer-received", "2026-01-11T00:00:00Z"),
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    assert rows == []


def test_pull_requests_excluded_from_issue_listing() -> None:
    """Issues API entries carrying a pull_request key are never treated as issues."""
    issues = [
        _issue(1, "Acme", "Engineer"),
        {**_issue(2, "PR Co", "N/A"), "pull_request": {"url": "..."}},
    ]
    events = {
        1: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"


def test_dedupe_same_issue_same_period_keeps_most_recent() -> None:
    """applied -> in-loop within one week collapses to a single in-loop row."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),  # Monday
            _labeled_event("in-loop", "2026-01-07T00:00:00Z"),  # Wednesday, same week
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert len(rows) == 1
    assert rows[0]["activity"] == "Interview scheduled"
    assert rows[0]["date"] == "2026-01-07"


def test_dedupe_preserves_separate_rows_across_periods() -> None:
    """An applied event and a later in-loop event in different weeks both survive."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),  # week of Jan 5
            _labeled_event("in-loop", "2026-01-20T00:00:00Z"),  # week of Jan 19
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert len(rows) == 2
    dates = sorted(row["date"] for row in rows)
    assert dates == ["2026-01-05", "2026-01-20"]


def test_dedupe_cross_period_scenario() -> None:
    """The trickiest dedupe case (specs/esd-export.md §6.1 step 5), named explicitly.

    One issue: a standalone `applied` event in an earlier period, then in a
    *later* period it moves applied -> in-loop within that same later period.
    Expected: two rows total -- the earlier-period `applied` row survives
    untouched, and the later period collapses to a single `in-loop` row
    (not two, and not losing the earlier row to a same-issue-wide dedupe).
    """
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),  # week of Jan 5
            _labeled_event("applied", "2026-01-20T00:00:00Z"),  # week of Jan 19
            _labeled_event("in-loop", "2026-01-22T00:00:00Z"),  # same week as above
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert len(rows) == 2
    by_date = {row["date"]: row for row in rows}
    assert by_date["2026-01-05"]["activity"] == "Applied online"
    assert by_date["2026-01-22"]["activity"] == "Interview scheduled"
    assert "2026-01-20" not in by_date  # collapsed into the in-loop row


def test_dedupe_ignores_unlabeled_events() -> None:
    """unlabeled (label removed) events never generate or count as activity."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),
            _unlabeled_event("applied", "2026-01-06T00:00:00Z"),
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    assert len(rows) == 1
    assert rows[0]["activity"] == "Applied online"


def test_weekly_bucketing_non_monday_week_start() -> None:
    """A Wednesday week_start groups events by Wed-to-Tue weeks, not Mon-Sun."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        # 2026-01-06 is a Tuesday; with week_start=wednesday it belongs to the
        # period starting 2025-12-31 (previous Wednesday), not 2026-01-05.
        1: [_labeled_event("applied", "2026-01-06T00:00:00Z")],
        # 2026-01-07 is a Wednesday itself -> starts a new period.
        2: [_labeled_event("applied", "2026-01-07T00:00:00Z")],
    }
    rows = export_rows(
        _make_client(issues, events),
        group_by="weekly",
        week_start="wednesday",
        max_per_period=None,
    )
    by_company = {row["company"]: row for row in rows}
    assert by_company["Acme"]["date"] == "2026-01-06"
    assert by_company["Beta"]["date"] == "2026-01-07"
    # Confirm they landed in different periods by checking a cap=1 run (on a
    # fresh client) keeps both -- same-period rows would compete for the cap.
    capped = export_rows(
        _make_client(issues, events),
        group_by="weekly",
        week_start="wednesday",
        max_per_period=1,
    )
    assert len(capped) == 2


def test_weekly_bucketing_event_on_week_start_day() -> None:
    """An event landing exactly on the week_start weekday starts its own period."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2026-01-06T12:00:00Z")]}  # a Tuesday
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="tuesday")
    assert rows[0]["date"] == "2026-01-06"


def test_weekly_bucketing_spans_year_boundary() -> None:
    """A week that crosses Dec 31 / Jan 1 still groups its events together."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        # Week of Monday 2025-12-29 runs through Sunday 2026-01-04.
        1: [_labeled_event("applied", "2025-12-31T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-02T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday", max_per_period=1)
    # Both land in the same period (week starting 2025-12-29); the cap keeps
    # only the earliest, proving they were grouped together.
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"


def test_monthly_bucketing_groups_by_calendar_month() -> None:
    """Monthly grouping ignores day-of-month and week_start entirely."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        1: [_labeled_event("applied", "2026-01-01T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-31T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="monthly", max_per_period=1)
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"


def test_period_label_weekly_monday_matches_iso_week() -> None:
    """With week_start=monday, the Period column is the real ISO week label."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2026-01-05T00:00:00Z")]}  # a Monday
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert rows[0]["period"] == "2026-W02"


def test_period_label_weekly_spans_year_boundary() -> None:
    """A week starting 2025-12-29 gets the ISO week/year it actually falls in."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2025-12-31T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="monday")
    assert rows[0]["period"] == "2026-W01"


def test_period_label_monthly_format() -> None:
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2026-01-31T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="monthly")
    assert rows[0]["period"] == "2026-01"


def test_period_label_non_monday_week_start_reflects_period_start() -> None:
    """A non-Monday week_start labels by the ISO week period_start falls in.

    specs/esd-export.md and _period_label's docstring both call this out:
    with a non-Monday week_start the custom period can span two ISO week
    numbers, so the label describes where the period *starts*, not
    necessarily every day inside it.
    """
    issues = [_issue(1, "Acme", "Engineer")]
    # 2026-01-06 (Tue) buckets to period_start 2025-12-31 (Wed) under
    # week_start=wednesday -- see test_weekly_bucketing_non_monday_week_start.
    events = {1: [_labeled_event("applied", "2026-01-06T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", week_start="wednesday")
    assert rows[0]["period"] == "2026-W01"  # the ISO week 2025-12-31 falls in


def test_cap_keeps_chronologically_earliest_rows() -> None:
    """When a period exceeds the cap, only the earliest N rows survive."""
    issues = [_issue(i, f"Company{i}", "Engineer") for i in range(1, 4)]
    events = {
        1: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-06T00:00:00Z")],
        3: [_labeled_event("applied", "2026-01-07T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", max_per_period=2)
    assert [row["company"] for row in rows] == ["Company1", "Company2"]


def test_cap_applies_to_deduped_rows_not_raw_events() -> None:
    """One issue with several raw events in a period still only ever costs 1 slot."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        # Three raw events on the same issue in one period -> one deduped row.
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-06T00:00:00Z"),
        ],
        2: [_labeled_event("applied", "2026-01-06T12:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", max_per_period=2)
    # Both issues fit within a cap of 2, proving issue 1 only consumed one slot
    # despite contributing two raw events.
    assert len(rows) == 2


def test_cap_tie_break_by_issue_number_ascending() -> None:
    """Identical timestamps across issues break ties by ascending issue number."""
    issues = [_issue(5, "Zeta", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        5: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", max_per_period=1)
    assert len(rows) == 1
    assert rows[0]["company"] == "Beta"  # issue 2 < issue 5


def test_unlimited_cap_keeps_every_row() -> None:
    """max_per_period=None keeps all deduped rows in a busy period."""
    issues = [_issue(i, f"Company{i}", "Engineer") for i in range(1, 6)]
    events = {
        i: [_labeled_event("applied", "2026-01-05T00:00:00Z")] for i in range(1, 6)
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly", max_per_period=None)
    assert len(rows) == 5


def test_date_range_filtering_boundary_inclusive() -> None:
    """Events exactly on start_date or end_date are included, not excluded."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        1: [_labeled_event("applied", "2026-01-01T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-10T23:59:59Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(
        client,
        group_by="weekly",
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 1, 10),
    )
    assert {row["company"] for row in rows} == {"Acme", "Beta"}


def test_date_range_filtering_excludes_outside_window() -> None:
    """Events before start_date or after end_date are dropped."""
    issues = [_issue(1, "Acme", "Engineer"), _issue(2, "Beta", "Analyst")]
    events = {
        1: [_labeled_event("applied", "2025-12-31T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-11T00:00:00Z")],
    }
    client = _make_client(issues, events)
    rows = export_rows(
        client,
        group_by="weekly",
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 1, 10),
    )
    assert rows == []


def test_end_date_defaults_to_today() -> None:
    """An event timestamped 'now' is included when end_date is omitted."""
    now = dt.datetime.now(dt.UTC)
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", now.isoformat().replace("+00:00", "Z"))]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    assert len(rows) == 1


def test_start_date_none_is_unbounded() -> None:
    """A start_date of None means no lower bound at all."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2020-01-01T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(
        client, group_by="weekly", start_date=None, end_date=dt.date(2026, 1, 1)
    )
    assert len(rows) == 1


def test_malformed_created_at_is_skipped() -> None:
    """An event with an unparseable or missing created_at is dropped, not raised."""
    issues = [_issue(1, "Acme", "Engineer")]
    events = {
        1: [
            {"event": "labeled", "label": "applied", "created_at": "not-a-date"},
            {"event": "labeled", "label": "applied", "created_at": None},
        ]
    }
    client = _make_client(issues, events)
    assert export_rows(client, group_by="weekly") == []


def test_activity_text_applied_ignores_source() -> None:
    """Activity is always "Applied online", never "via {source}".

    `source` only records which job board the *listing* was scraped from,
    not how the claimant actually submitted the application (often a
    company's own site, not the board it was found on) -- naming a specific
    channel would overclaim in a document filed with a government agency.
    A malicious-looking source value also confirms nothing from it can leak
    into (or open as a formula in) the Activity cell, since it's never used.
    """
    issues = [_issue(1, "Acme", "Engineer", source="=cmd|'/c calc'!A1")]
    events = {1: [_labeled_event("applied", "2026-01-05T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    assert rows[0]["activity"] == "Applied online"


def test_activity_text_applied_without_source() -> None:
    issues = [_issue(1, "Acme", "Engineer")]
    events = {1: [_labeled_event("applied", "2026-01-05T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    assert rows[0]["activity"] == "Applied online"


def test_applied_row_includes_apply_url_in_loop_row_does_not() -> None:
    issues = [_issue(1, "Acme", "Engineer", url="https://acme.example/apply")]
    events = {
        1: [
            _labeled_event("applied", "2026-01-05T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-20T00:00:00Z"),
        ]
    }
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    by_date = {row["date"]: row for row in rows}
    assert by_date["2026-01-05"]["apply_url"] == "https://acme.example/apply"
    assert by_date["2026-01-20"]["apply_url"] == ""
    assert by_date["2026-01-20"]["activity"] == "Interview scheduled"


def test_formula_leading_characters_are_neutralized() -> None:
    """company/position starting with =+-@ get a defensive leading-apostrophe prefix.

    apply_url isn't exercised here with a formula-leading value: parse_job_details's
    own _sanitize_apply_url already requires a URL to start with http(s):// and
    rejects quote/paren characters, so it can never itself start with =+-@ -- the
    _neutralize_formula call on it in _build_row is defense-in-depth for a case
    that can't currently occur, not something this test can trigger end-to-end.
    """
    issues = [
        {
            "number": 1,
            "title": "[=cmd|'/c calc'!A1] +HYPERLINK(\"evil\")",
            "body": (
                "## Job Description\nBody.\n\n"
                "**Company:** =cmd|'/c calc'!A1\n"
                '**Role:** +HYPERLINK("evil")'
            ),
        }
    ]
    events = {1: [_labeled_event("applied", "2026-01-05T00:00:00Z")]}
    client = _make_client(issues, events)
    rows = export_rows(client, group_by="weekly")
    row = rows[0]
    assert row["company"].startswith("'=")
    assert row["position"].startswith("'+")


def test_unhandled_activity_label_raises_instead_of_silently_mislabeling() -> None:
    """A label outside {"applied", "in-loop"} fails loudly, not silently.

    ACTIVITY_LABELS filtering in _fetch_activity_events already excludes any
    label but these two before _build_row ever sees one, so this reaches
    _build_row directly to exercise its own defense-in-depth guard.
    """
    issue = _issue(1, "Acme", "Engineer")
    event = _ActivityEvent(
        1, "some-future-label", dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    )
    with pytest.raises(AssertionError, match="Unhandled activity label"):
        _build_row(issue, event, period_label="2026-W02")


def test_issue_listing_paginates_fully() -> None:
    """A full first page of issues triggers a second list_issues call."""
    page1 = [_issue(i, f"Company{i}", "Engineer") for i in range(1, 101)]
    page2 = [_issue(101, "Company101", "Engineer")]
    events = {
        i: [_labeled_event("applied", "2026-01-05T00:00:00Z")] for i in range(1, 102)
    }
    client = MagicMock()
    client.list_issues.side_effect = [page1, page2, []]
    client.list_issue_label_events.side_effect = lambda issue_number: events.get(
        issue_number, []
    )
    rows = export_rows(client, group_by="weekly", max_per_period=None)
    assert len(rows) == 101
    assert client.list_issues.call_count == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"group_by": "daily"}, "group_by must be"),
        ({"group_by": "weekly", "week_start": "someday"}, "week_start must be"),
        (
            {"group_by": "weekly", "max_per_period": 0},
            "max_per_period must be positive",
        ),
        (
            {"group_by": "weekly", "max_per_period": -1},
            "max_per_period must be positive",
        ),
    ],
)
def test_invalid_arguments_raise_value_error(kwargs: dict, message: str) -> None:
    client = _make_client([], {})
    with pytest.raises(ValueError, match=message):
        export_rows(client, **kwargs)


# --- write_rows -------------------------------------------------------------

_SAMPLE_ROWS = [
    {
        "period": "2026-W02",
        "date": "2026-01-05",
        "company": "Acme, Inc.",
        "position": 'Senior "Backend" Engineer',
        "activity": "Applied online",
        "apply_url": "https://acme.example/apply",
    },
    {
        "period": "2026-W04",
        "date": "2026-01-20",
        "company": "Bëta Söftwäre\nGmbH",
        "position": "Analyst",
        "activity": "Interview scheduled",
        "apply_url": "",
    },
]

_EXPECTED_FRAME = pd.DataFrame(_SAMPLE_ROWS).rename(
    columns={
        "period": "Period",
        "date": "Date",
        "company": "Company",
        "position": "Position",
        "activity": "Activity",
        "apply_url": "Application URL",
    }
)


def test_write_rows_csv_round_trips(tmp_path) -> None:
    output = tmp_path / "export.csv"
    write_rows(_SAMPLE_ROWS, output, "csv")

    # keep_default_na=False: read the written empty apply_url cell back as ""
    # rather than pandas' default NaN-on-read interpretation of a blank field
    # (a *reading* convention, not evidence of what write_rows actually wrote).
    frame = pd.read_csv(output, keep_default_na=False, dtype=str)
    pd.testing.assert_frame_equal(frame, _EXPECTED_FRAME)


def test_write_rows_xlsx_round_trips(tmp_path) -> None:
    output = tmp_path / "export.xlsx"
    write_rows(_SAMPLE_ROWS, output, "xlsx")

    frame = pd.read_excel(output, keep_default_na=False, dtype=str)
    pd.testing.assert_frame_equal(frame, _EXPECTED_FRAME)


def test_write_rows_creates_missing_parent_directory(tmp_path) -> None:
    output = tmp_path / "nested" / "dir" / "export.csv"
    write_rows(_SAMPLE_ROWS, output, "csv")
    assert output.exists()


def test_write_rows_empty_csv_has_headers_only(tmp_path) -> None:
    output = tmp_path / "export.csv"
    write_rows([], output, "csv")

    frame = pd.read_csv(output)
    assert list(frame.columns) == [
        "Period",
        "Date",
        "Company",
        "Position",
        "Activity",
        "Application URL",
    ]
    assert len(frame) == 0


def test_write_rows_empty_xlsx_has_headers_only(tmp_path) -> None:
    output = tmp_path / "export.xlsx"
    write_rows([], output, "xlsx")

    frame = pd.read_excel(output)
    assert list(frame.columns) == [
        "Period",
        "Date",
        "Company",
        "Position",
        "Activity",
        "Application URL",
    ]
    assert len(frame) == 0


def test_write_rows_invalid_format_raises() -> None:
    with pytest.raises(ValueError, match="fmt must be one of"):
        write_rows(_SAMPLE_ROWS, "/dev/null", "pdf")
