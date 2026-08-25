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

# Maps each triage fit dimension to the label applied when its score is too low.
FIT_CATEGORY_MISMATCH_LABELS: dict[str, str] = {
    "tech_stack_fit": "tech-stack-mismatch",
    "experience_fit": "experience-mismatch",
    "location_fit": "location-mismatch",
    "salary_fit": "salary-mismatch",
    "industry_fit": "industry-mismatch",
}

# Mismatch reason labels applied by triage.py when scoring below threshold.
MISMATCH_REASON_LABELS: frozenset[str] = frozenset(
    FIT_CATEGORY_MISMATCH_LABELS.values()
)


def is_lifecycle_label_satisfied(target_label: str, current_labels: set[str]) -> bool:
    """Check if the target lifecycle label is satisfied by the current labels.

    For 'triage-mismatched', it is satisfied if 'triage-mismatched' or any
    specific mismatch reason label is present, and no other lifecycle labels
    are present. For other labels, it requires the exact label and no siblings.
    """
    if target_label == "triage-mismatched":
        has_mismatch = (
            "triage-mismatched" in current_labels
            or not MISMATCH_REASON_LABELS.isdisjoint(current_labels)
        )
        siblings_to_remove = LIFECYCLE_LABELS & current_labels - {"triage-mismatched"}
        return has_mismatch and not siblings_to_remove
    return target_label in current_labels and not (
        LIFECYCLE_LABELS & current_labels - {target_label}
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

    # Skip adding the generic 'triage-mismatched' label if a specific mismatch reason
    # is already present to prevent redundant label clutter.
    if target_label == "triage-mismatched" and not MISMATCH_REASON_LABELS.isdisjoint(
        current_labels
    ):
        return

    if target_label not in current_labels:
        gh_client.add_labels(issue_number, [target_label])


# Terminal lifecycle labels: adding one also closes its issue. Exactly the
# value domain of resolve_closed_lifecycle_label below — kept beside it so
# the two cannot drift apart.
CLOSURE_LABELS: frozenset[str] = frozenset({"rejected", "triage-mismatched"})


def resolve_closed_lifecycle_label(labels: set[str]) -> str:
    """Resolve the lifecycle label for a closed issue.

    Returns 'triage-mismatched' if that label or any specific mismatch label is
    present; otherwise defaults to 'rejected'.
    """
    if "triage-mismatched" in labels or not MISMATCH_REASON_LABELS.isdisjoint(labels):
        return "triage-mismatched"
    return "rejected"


def get_updated_lifecycle_labels(
    target_label: str, current_labels: set[str]
) -> set[str]:
    """Calculate the updated set of labels after applying target lifecycle label."""
    updated = current_labels - LIFECYCLE_LABELS
    if not (
        target_label == "triage-mismatched"
        and not MISMATCH_REASON_LABELS.isdisjoint(updated)
    ):
        updated.add(target_label)
    return updated
