"""Intent detection helpers for JobGitOps."""

APPLIED_KEYWORDS = ("applied", "interview", "loop", "screen", "offer")


def detect_applied_intent(title: str, body: str) -> bool:
    """Detect if the user's issue title or body indicates they have already applied."""
    content_lower = (title or "").lower() + " " + (body or "").lower()
    return any(kw in content_lower for kw in APPLIED_KEYWORDS)
