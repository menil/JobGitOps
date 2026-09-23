"""Job-search activity export for filing with a state unemployment department.

See specs/esd-export.md for the full design. This module implements the
pipeline: fetch issues, reconstruct activity history from GitHub's issue
timeline, bucket into reporting periods, dedupe, and cap -- producing the
row list the CLI (jobgitops/cli/esd_export.py) hands to a spreadsheet writer.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from jobgitops.cli.triage import parse_job_details
from jobgitops.github_client import GitHubClient
from jobgitops.status_model import ACTIVITY_LABELS

WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_ISSUE_PAGE_SIZE = 100

_ACTIVITY_TEXT_IN_LOOP = "Interviewed for position"


@dataclass(frozen=True)
class _ActivityEvent:
    """One qualifying label-add event, resolved to a UTC-aware timestamp."""

    issue_number: int
    label: str
    created_at: dt.datetime


def export_rows(
    gh_client: GitHubClient,
    *,
    group_by: str,
    week_start: str = "monday",
    max_per_period: int | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[dict[str, str]]:
    """Build ESD export rows (specs/esd-export.md §6.1).

    Args:
        gh_client: Client used to list issues and their label-event history.
        group_by: Reporting period granularity, ``"weekly"`` or ``"monthly"``.
        week_start: Weekday name (see ``WEEKDAYS``) the week starts on;
            ignored when ``group_by="monthly"``.
        max_per_period: Row cap per period, or ``None`` for unlimited. When a
            period exceeds the cap, the chronologically earliest rows are
            kept.
        start_date: Optional inclusive lower bound; ``None`` means unbounded.
        end_date: Optional inclusive upper bound; ``None`` defaults to today
            (UTC).

    Returns:
        Rows sorted by period then date, each a dict with keys ``date``,
        ``company``, ``position``, ``activity``, ``apply_url``.

    Raises:
        ValueError: If ``group_by``, ``week_start``, or ``max_per_period`` is
            invalid.
    """
    if group_by not in ("weekly", "monthly"):
        raise ValueError(f"group_by must be 'weekly' or 'monthly', got {group_by!r}")
    week_start_key = week_start.lower()
    if week_start_key not in WEEKDAYS:
        raise ValueError(f"week_start must be a weekday name, got {week_start!r}")
    if max_per_period is not None and max_per_period <= 0:
        raise ValueError(f"max_per_period must be positive, got {max_per_period}")

    resolved_end_date = end_date or dt.datetime.now(dt.UTC).date()
    week_start_index = WEEKDAYS.index(week_start_key)

    issues = _list_all_issues(gh_client)
    events = _fetch_activity_events(gh_client, issues, start_date, resolved_end_date)
    winners = _dedupe_per_period(events, group_by, week_start_index)
    kept = _apply_cap(winners, max_per_period)

    issues_by_number = {issue["number"]: issue for issue in issues}
    return [
        _build_row(issues_by_number[issue_number], event)
        for _period, issue_number, event in kept
    ]


def _list_all_issues(gh_client: GitHubClient) -> list[dict[str, Any]]:
    """Fetch every issue (open and closed), filtering out pull requests.

    Mirrors ``project_sync._list_all_issues``: the Issues REST API also
    returns pull requests, which this export never treats as job listings.
    Unlike the scraper's last-100-issues dedup cache, this needs complete
    history, so it pages until a short page.
    """
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_client.list_issues(state="all", per_page=_ISSUE_PAGE_SIZE, page=page)
        if not batch:
            break
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < _ISSUE_PAGE_SIZE:
            break
        page += 1
    return issues


def _fetch_activity_events(
    gh_client: GitHubClient,
    issues: list[dict[str, Any]],
    start_date: dt.date | None,
    end_date: dt.date,
) -> list[_ActivityEvent]:
    """Collect activity-label-add events across all issues, date-filtered."""
    events: list[_ActivityEvent] = []
    for issue in issues:
        for raw in gh_client.list_issue_label_events(issue["number"]):
            if raw.get("event") != "labeled" or raw.get("label") not in ACTIVITY_LABELS:
                continue
            created_at = _parse_iso(raw.get("created_at"))
            if created_at is None:
                continue
            event_date = created_at.astimezone(dt.UTC).date()
            if start_date is not None and event_date < start_date:
                continue
            if event_date > end_date:
                continue
            events.append(_ActivityEvent(issue["number"], raw["label"], created_at))
    return events


def _period_start(event_date: dt.date, group_by: str, week_start_index: int) -> dt.date:
    """Resolve the UTC-calendar-date reporting period an event falls into."""
    if group_by == "monthly":
        return event_date.replace(day=1)
    days_since_week_start = (event_date.weekday() - week_start_index) % 7
    return event_date - dt.timedelta(days=days_since_week_start)


def _dedupe_per_period(
    events: list[_ActivityEvent], group_by: str, week_start_index: int
) -> dict[tuple[int, dt.date], _ActivityEvent]:
    """Collapse same-issue, same-period events to the most recent one.

    An issue that moved applied -> in-loop within one period reports the
    in-loop activity for that period; an applied event in an earlier period
    still survives as its own entry, keyed by its own (issue, period).
    """
    winners: dict[tuple[int, dt.date], _ActivityEvent] = {}
    for event in events:
        period_start = _period_start(
            event.created_at.astimezone(dt.UTC).date(), group_by, week_start_index
        )
        key = (event.issue_number, period_start)
        current = winners.get(key)
        if current is None or event.created_at > current.created_at:
            winners[key] = event
    return winners


def _apply_cap(
    winners: dict[tuple[int, dt.date], _ActivityEvent], max_per_period: int | None
) -> list[tuple[dt.date, int, _ActivityEvent]]:
    """Cap rows per period to the chronologically earliest ``max_per_period``.

    Ties (identical timestamps within a period, across different issues)
    break by issue number ascending, for deterministic output.
    """
    by_period: dict[dt.date, list[tuple[int, _ActivityEvent]]] = defaultdict(list)
    for (issue_number, period_start), event in winners.items():
        by_period[period_start].append((issue_number, event))

    kept: list[tuple[dt.date, int, _ActivityEvent]] = []
    for period_start, entries in by_period.items():
        entries.sort(key=lambda pair: (pair[1].created_at, pair[0]))
        if max_per_period is not None:
            entries = entries[:max_per_period]
        kept.extend(
            (period_start, issue_number, event) for issue_number, event in entries
        )

    kept.sort(key=lambda row: (row[0], row[2].created_at, row[1]))
    return kept


def _build_row(issue: dict[str, Any], event: _ActivityEvent) -> dict[str, str]:
    """Resolve one export row (specs/esd-export.md §7) for a winning event."""
    details = parse_job_details(issue.get("body"), issue.get("title"))
    if event.label == "applied":
        source = details.get("source", "")
        activity = f"Applied online via {source}" if source else "Applied online"
        apply_url = details.get("apply_url", "")
    elif event.label == "in-loop":
        activity = _ACTIVITY_TEXT_IN_LOOP
        apply_url = ""
    else:
        # ACTIVITY_LABELS (status_model.py) currently has exactly these two
        # members; fail loudly rather than silently mislabeling a future
        # third label as "Interviewed for position" in a filing someone
        # submits to a government agency.
        raise AssertionError(
            f"Unhandled activity label {event.label!r}; ACTIVITY_LABELS grew "
            "without a matching _build_row branch."
        )
    return {
        "date": event.created_at.astimezone(dt.UTC).date().isoformat(),
        "company": _neutralize_formula(details.get("company", "")),
        "position": _neutralize_formula(details.get("role", "")),
        "activity": activity,
        "apply_url": _neutralize_formula(apply_url),
    }


def _neutralize_formula(value: str) -> str:
    """Prefix a leading `'` if `value` would open as a spreadsheet formula.

    `company`/`position`/`apply_url` are extracted from GitHub issue bodies,
    which ultimately originate from scraped, external job postings (see
    scraper.py) -- not fully trusted content. A cell starting with
    `= + - @` opens as a formula in Excel/most spreadsheet apps; this file
    is filed with a government agency and opened by the claimant themselves,
    so the leading apostrophe (the standard CSV/XLSX formula-injection
    mitigation) is a cheap, harmless precaution even though the blast radius
    here is limited to the claimant's own machine.
    """
    if value and value[0] in "=+-@":
        return f"'{value}"
    return value


def _parse_iso(value: str | None) -> dt.datetime | None:
    """Parse a GitHub ISO 8601 timestamp, or None for anything unparseable."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
