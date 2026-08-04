"""Canonical mapping between job-lifecycle labels and Projects V2 statuses.

The whole pipeline (scraper, triage, status-transition, project-sync) must agree
on which lifecycle label maps to which board column. Historically those names
were hardcoded in every script, which is why the automation could drift from the
actual Status field options on the board. This module is the single source of
truth every caller imports.
"""

# Lifecycle labels are mutually exclusive: an issue represents exactly one
# pipeline stage, so sync code can safely enforce exclusivity against this set.
LIFECYCLE_LABELS: frozenset[str] = frozenset(
    {
        "triage-pending",
        "ready-to-apply",
        "applied",
        "in-loop",
        "offer-received",
        "rejected",
        "triage-mismatched",
    }
)

# Lifecycle label -> Projects V2 Status field option name (board column).
LABEL_TO_STATUS: dict[str, str] = {
    "triage-pending": "Triage Pending",
    "ready-to-apply": "Ready to Apply",
    "applied": "Applied",
    "in-loop": "In Loop",
    "offer-received": "Offer Received",
    "rejected": "Rejected",
    "triage-mismatched": "Mismatched/Closed",
}

# Status option name -> lifecycle label (inverse of LABEL_TO_STATUS).
STATUS_TO_LABEL: dict[str, str] = {
    status: label for label, status in LABEL_TO_STATUS.items()
}

# Statuses the reverse (column -> label) sync is allowed to act on. Triage
# Pending is excluded deliberately: dragging a card back to it must not re-add
# the triage-pending label, which would re-trigger a full AI re-triage via
# triage-issue.yml. The forward (label -> column) direction still covers it.
REVERSE_SYNC_STATUSES: frozenset[str] = frozenset(STATUS_TO_LABEL) - frozenset(
    {"Triage Pending"}
)


def sync_lifecycle_label(
    gh_client: object,
    issue_number: int,
    target_label: str,
    current_labels: set[str] | None = None,
) -> None:
    """Make ``target_label`` the only lifecycle label on an issue.

    Removes every other ``LIFECYCLE_LABELS`` entry first, then adds the target
    (skipping the add when it is already present, so re-runs are no-ops).
    Non-lifecycle labels (fit tiers, mismatch reasons) are left untouched.
    Pass a pre-fetched label set as ``current_labels`` to avoid a redundant
    API round-trip; when omitted it is fetched once.

    Args:
        gh_client: Object exposing ``get_labels``, ``remove_label``, and
            ``add_labels`` (a ``jobgitops.github_client.GitHubClient`` or test
            double).
        issue_number: GitHub issue number to update.
        target_label: The lifecycle label that should remain.
        current_labels: Optional label set already known to the caller. When
            given, it is assumed to be an up-to-date snapshot; otherwise it is
            fetched from the API.
    """
    if current_labels is None:
        current_labels = set(gh_client.get_labels(issue_number))
    stale = sorted(
        label
        for label in current_labels
        if label in LIFECYCLE_LABELS and label != target_label
    )
    for label in stale:
        gh_client.remove_label(issue_number, label)
    if target_label not in current_labels:
        gh_client.add_labels(issue_number, [target_label])


def resolve_closed_lifecycle_label(labels: set[str]) -> str:
    """Resolve the lifecycle label for a closed issue.

    Returns 'triage-mismatched' if that label is present; otherwise defaults to
    'rejected'.
    """
    if "triage-mismatched" in labels:
        return "triage-mismatched"
    return "rejected"
