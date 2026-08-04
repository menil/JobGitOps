"""Unit tests for the canonical status/label model."""

from jobgitops.status_model import (
    LABEL_TO_STATUS,
    LIFECYCLE_LABELS,
    REVERSE_SYNC_STATUSES,
    STATUS_TO_LABEL,
    sync_lifecycle_label,
)


def test_lifecycle_labels_are_complete() -> None:
    assert {
        "triage-pending",
        "ready-to-apply",
        "applied",
        "in-loop",
        "offer-received",
        "rejected",
        "triage-mismatched",
    } == LIFECYCLE_LABELS


def test_label_to_status_mapping() -> None:
    assert LABEL_TO_STATUS == {
        "triage-pending": "Triage Pending",
        "ready-to-apply": "Ready to Apply",
        "applied": "Applied",
        "in-loop": "In Loop",
        "offer-received": "Offer Received",
        "rejected": "Rejected",
        "triage-mismatched": "Mismatched/Closed",
    }


def test_status_to_label_is_exact_inverse() -> None:
    assert {
        status: label for label, status in LABEL_TO_STATUS.items()
    } == STATUS_TO_LABEL
    assert len(STATUS_TO_LABEL) == len(LABEL_TO_STATUS)


def test_reverse_sync_excludes_triage_pending() -> None:
    assert "Triage Pending" not in REVERSE_SYNC_STATUSES
    assert set(STATUS_TO_LABEL) - {"Triage Pending"} == REVERSE_SYNC_STATUSES


class FakeClient:
    """In-memory GitHub client double tracking lifecycle-label side effects."""

    def __init__(self, labels: list[str] | None = None) -> None:
        self.labels = set(labels or [])
        self.removed: list[str] = []
        self.added: list[str] = []

    def get_labels(self, issue_number: int) -> list[str]:
        return list(self.labels)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.labels.discard(label)
        self.removed.append(label)

    def add_labels(self, issue_number: int, labels: list[str]) -> list[str]:
        self.labels.update(labels)
        self.added.extend(labels)
        return list(self.labels)


def test_sync_lifecycle_label_adds_target_and_removes_siblings() -> None:
    client = FakeClient(["triage-pending", "fit:A"])
    sync_lifecycle_label(client, 7, "applied")
    assert client.labels == {"fit:A", "applied"}
    assert client.removed == ["triage-pending"]
    assert client.added == ["applied"]


def test_sync_lifecycle_label_preserves_non_lifecycle_labels() -> None:
    client = FakeClient(["fit:A+", "salary-mismatch"])
    sync_lifecycle_label(client, 3, "ready-to-apply")
    assert client.labels == {"fit:A+", "salary-mismatch", "ready-to-apply"}
    assert client.removed == []


def test_sync_lifecycle_label_noop_when_only_target_present() -> None:
    client = FakeClient(["applied"])
    sync_lifecycle_label(client, 9, "applied")
    assert client.labels == {"applied"}
    assert client.removed == []
    assert client.added == []


def test_sync_lifecycle_label_unknown_target_still_cleans_siblings() -> None:
    client = FakeClient(["applied", "fit:B"])
    sync_lifecycle_label(client, 11, "triage-mismatched")
    assert client.labels == {"fit:B", "triage-mismatched"}


def test_sync_lifecycle_label_accepts_prefetched_labels() -> None:
    """Verify a caller-supplied label set skips the get_labels round-trip."""
    client = FakeClient(["in-loop", "fit:A"])
    fetched = client.get_labels(11)
    client.get_labels = None  # type: ignore[assignment]

    sync_lifecycle_label(client, 11, "applied", set(fetched))

    assert client.labels == {"fit:A", "applied"}
    assert client.removed == ["in-loop"]
    assert client.added == ["applied"]
