"""Tests for already-applied intent detection."""

from jobgitops.intent import detect_applied_intent


def test_detect_applied_intent_positive() -> None:
    assert detect_applied_intent("I applied to Google", "") is True
    assert (
        detect_applied_intent("", "Looking forward to my interview loop next week")
        is True
    )
    assert detect_applied_intent("Acme screen scheduled", "") is True
    assert detect_applied_intent("", "They extended an offer!") is True


def test_detect_applied_intent_negative() -> None:
    assert (
        detect_applied_intent(
            "Need to triage this job description", "Here is the URL: ..."
        )
        is False
    )
    assert detect_applied_intent("", "") is False
