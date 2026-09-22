"""Unit tests for the Gmail sync orchestrator CLI entry point (spec §5.2, §10).

Reuses `FakeGitHubClient`/`sample_resume()`/`sample_settings()` from
`tests/test_respond.py` rather than reinventing them, extending
`FakeGitHubClient` only with the `list_issues`/`get_issue` surface this
module touches that `respond.py`'s own tests never needed.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobgitops.assistant import (
    GMAIL_NOTICE_MARKER,
    STATUS_CONFIRMATION_MARKER,
    STATUS_LABELS,
)
from jobgitops.cli import gmail_sync
from jobgitops.git_ops import GitOpsError
from jobgitops.github_client import GitHubClientError
from jobgitops.gmail_client import GmailMessageNotFoundError, Message
from jobgitops.gmail_match import Candidate, PreFilterResult
from jobgitops.llm import EmailMatchResult, QuotaExceededError, ValidationError
from jobgitops.schema import GmailConfig, Settings
from tests.test_respond import (
    DEFAULT_ENV,
    FakeGitHubClient,
    sample_resume,
    sample_settings,
)

GMAIL_ENV = {
    **DEFAULT_ENV,
    "GMAIL_CLIENT_ID": "client-id",
    "GMAIL_CLIENT_SECRET": "client-secret",
    "GMAIL_REFRESH_TOKEN": "refresh-token",
}


def gmail_settings(**overrides: object) -> Settings:
    """`sample_settings()` extended with an enabled Gmail config."""
    settings = sample_settings()
    config_kwargs = {"enabled": True, "label": "JobGitOps", "days_back": 7}
    config_kwargs.update(overrides)
    settings.gmail = GmailConfig(**config_kwargs)
    return settings


def _passing_auth_results() -> list[str]:
    return ["mx.google.com;\n       dmarc=pass (p=REJECT) header.from=acme.com"]


def _failing_auth_results() -> list[str]:
    return ["mx.google.com;\n       dmarc=fail (p=REJECT) header.from=acme.com"]


def make_message(
    message_id: str = "msg-1",
    subject: str = "Update on your application",
    sender: str = "HR <hr@acme.com>",
    body: str = "We wanted to update you on your application.",
    date: str = "Fri, 19 Sep 2026 10:00:00 +0000",
    authentic: bool = True,
) -> Message:
    """Build a `Message` for orchestration tests."""
    return Message(
        message_id=message_id,
        date=date,
        subject=subject,
        sender=sender,
        raw_auth_results=_passing_auth_results()
        if authentic
        else _failing_auth_results(),
        body_text=body,
    )


def make_candidate(number: int, title: str = "Staff Engineer at Acme") -> Candidate:
    return Candidate(
        number=number,
        title=title,
        company="Acme Corp",
        role="Staff Engineer",
        apply_url="https://boards.greenhouse.io/acme-corp/jobs/1",
    )


def gh_issue(
    number: int, node_id: str = "ND_1", labels: list[str] | None = None
) -> dict:
    """Build a minimal raw GitHub issue payload, as `GitHubClient.get_issue` returns."""
    return {
        "number": number,
        "node_id": node_id,
        "labels": [{"name": label} for label in (labels or ["applied"])],
    }


class FakeGmailGitHubClient(FakeGitHubClient):
    """`FakeGitHubClient` extended with `list_issues`/`get_issue`, the two
    calls `gmail_sync.py` needs that `respond.py`'s own tests never
    exercised."""

    def __init__(self, *, issues: list[dict] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.issues = list(issues or [])

    def list_issues(
        self,
        state: str = "open",
        per_page: int = 100,
        page: int | None = None,
        labels: str | None = None,
        sort: str | None = None,
        direction: str | None = None,
    ) -> list[dict]:
        if page and page > 1:
            return []
        return list(self.issues)

    def get_issue(self, issue_number: int) -> dict:
        for issue in self.issues:
            if issue.get("number") == issue_number:
                return issue
        raise GitHubClientError(f"Issue #{issue_number} not found.")


class FakeGmailClient:
    """In-memory `GmailClient` double for orchestration tests."""

    def __init__(
        self,
        *,
        label_id: str | None = "Label_1",
        message_ids: list[str] | None = None,
        messages: dict[str, Message] | None = None,
    ) -> None:
        self.label_id = label_id
        self.message_ids = list(message_ids or [])
        self.messages = dict(messages or {})
        self.resolve_calls: list[str] = []
        self.list_calls: list[tuple] = []

    def resolve_label_id(self, label_name: str) -> str | None:
        self.resolve_calls.append(label_name)
        return self.label_id

    def list_message_ids(
        self, label_id: str, query: str | None, days_back: int
    ) -> list[str]:
        self.list_calls.append((label_id, query, days_back))
        return list(self.message_ids)

    def get_message(self, message_id: str) -> Message:
        if message_id not in self.messages:
            raise GmailMessageNotFoundError(f"{message_id} not found")
        return self.messages[message_id]


# --- main(): no-op / fatal-error gating (spec §4.1/§5.2 step 1) -------------


def test_main_exits_0_when_gmail_section_absent(tmp_path: Path) -> None:
    """No `settings.gmail` at all is a clean, silent no-op."""
    with (
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=sample_settings()),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 0


def test_main_exits_0_when_gmail_disabled(tmp_path: Path) -> None:
    """`settings.gmail.enabled: false` is also a clean, silent no-op."""
    settings = gmail_settings(enabled=False)
    with (
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 0


@pytest.mark.parametrize(
    "missing_env",
    [
        {"GMAIL_CLIENT_SECRET": "x", "GMAIL_REFRESH_TOKEN": "y"},
        {"GMAIL_CLIENT_ID": "x", "GMAIL_REFRESH_TOKEN": "y"},
        {"GMAIL_CLIENT_ID": "x", "GMAIL_CLIENT_SECRET": "y"},
        {},
    ],
)
def test_main_exits_1_when_enabled_with_missing_secrets(
    tmp_path: Path, missing_env: dict
) -> None:
    """Enabled Gmail config with any missing required secret is fatal (exit 1)."""
    settings = gmail_settings()
    env = {**DEFAULT_ENV, **missing_env}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_settings_fail_to_load(tmp_path: Path) -> None:
    """A settings load failure is fatal (exit 1), same as respond.py."""
    with (
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch(
            "jobgitops.cli.gmail_sync.load_settings",
            side_effect=RuntimeError("bad yaml"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_github_token_missing(tmp_path: Path) -> None:
    """Enabled Gmail config with secrets present but GITHUB_TOKEN missing is fatal."""
    settings = gmail_settings()
    env = {k: v for k, v in GMAIL_ENV.items() if k != "GITHUB_TOKEN"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_github_repository_missing(tmp_path: Path) -> None:
    """Enabled Gmail config with secrets present but GITHUB_REPOSITORY missing
    is fatal (mirrors GITHUB_TOKEN's own check just above it)."""
    settings = gmail_settings()
    env = {k: v for k, v in GMAIL_ENV.items() if k != "GITHUB_REPOSITORY"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_quota_exceeded_exits_75(tmp_path: Path) -> None:
    """A QuotaExceededError from run_sync exits 75 (mirrors test_respond.py)."""
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            return_value=FakeGmailGitHubClient(),
        ),
        patch("jobgitops.cli.gmail_sync.GmailClient", return_value=FakeGmailClient()),
        patch("jobgitops.cli.gmail_sync.get_llm_client", return_value=MagicMock()),
        patch(
            "jobgitops.cli.gmail_sync.run_sync",
            side_effect=QuotaExceededError("quota"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 75


def test_main_fatal_label_error_exits_1(tmp_path: Path) -> None:
    """A GmailSyncFatalError from run_sync (label not found) exits 1."""
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            return_value=FakeGmailGitHubClient(),
        ),
        patch("jobgitops.cli.gmail_sync.GmailClient", return_value=FakeGmailClient()),
        patch("jobgitops.cli.gmail_sync.get_llm_client", return_value=MagicMock()),
        patch(
            "jobgitops.cli.gmail_sync.run_sync",
            side_effect=gmail_sync.GmailSyncFatalError("no such label"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


# --- Staleness warning (spec §5.2 step 2) ------------------------------------


def test_check_staleness_warns_when_last_synced_at_absent(
    capsys: pytest.CaptureFixture,
) -> None:
    """A missing last_synced_at (first run) is treated as unknown/stale."""
    gmail_sync._check_staleness(None, days_back=7, now=datetime.now(UTC))
    assert "::warning::" in capsys.readouterr().out


def test_check_staleness_warns_when_gap_exceeds_days_back(
    capsys: pytest.CaptureFixture,
) -> None:
    """A last_synced_at older than days_back triggers the warning annotation."""
    now = datetime.now(UTC)
    stale = now - timedelta(days=10)
    gmail_sync._check_staleness(gmail_sync._format_iso(stale), days_back=7, now=now)
    out = capsys.readouterr().out
    assert out.startswith("::warning::")


def test_check_staleness_silent_when_recent(capsys: pytest.CaptureFixture) -> None:
    """A recent last_synced_at within days_back logs no warning."""
    now = datetime.now(UTC)
    recent = now - timedelta(hours=1)
    gmail_sync._check_staleness(gmail_sync._format_iso(recent), days_back=7, now=now)
    assert "::warning::" not in capsys.readouterr().out


# --- Per-run cap / oldest-first selection (spec §4.1/§5.2 step 4) -----------


def test_select_batch_filters_already_processed() -> None:
    """Already-processed message IDs are dropped entirely."""
    result = gmail_sync._select_batch(["a", "b", "c"], {"b": "2026-01-01T00:00:00Z"})
    assert result == ["c", "a"]


def test_select_batch_caps_to_max_messages_oldest_first() -> None:
    """More than MAX_MESSAGES_PER_RUN eligible IDs: only the oldest N are
    returned, leaving the remainder for the next run."""
    ids = [f"m{i}" for i in range(gmail_sync.MAX_MESSAGES_PER_RUN + 10)]
    result = gmail_sync._select_batch(ids, {})
    assert len(result) == gmail_sync.MAX_MESSAGES_PER_RUN
    # Gmail returns newest-first; oldest-first means the tail of `ids` first.
    assert result[0] == ids[-1]
    assert result[-1] == ids[-gmail_sync.MAX_MESSAGES_PER_RUN]


# --- Cursor pruning (spec §5.2 step 6) ---------------------------------------


def test_prune_processed_drops_entries_older_than_days_back_plus_one() -> None:
    """Entries older than days_back + 1 days are dropped; newer ones kept."""
    # Rounded to whole seconds so the boundary entry's ISO-formatted
    # timestamp (which also truncates to whole seconds) compares exactly
    # equal to the cutoff, rather than landing a fraction of a second short.
    now = datetime.now(UTC).replace(microsecond=0)
    days_back = 7
    old_date = gmail_sync._format_iso(now - timedelta(days=days_back + 2))
    boundary_date = gmail_sync._format_iso(now - timedelta(days=days_back + 1))
    recent_date = gmail_sync._format_iso(now - timedelta(days=1))
    processed = {"old": old_date, "boundary": boundary_date, "recent": recent_date}

    pruned = gmail_sync._prune_processed(processed, days_back, now)

    assert "old" not in pruned
    assert "boundary" in pruned
    assert "recent" in pruned


def test_prune_processed_keeps_unparseable_dates() -> None:
    """An entry with an unparseable date is kept, not dropped (fail safe)."""
    pruned = gmail_sync._prune_processed({"weird": "not-a-date"}, 7, datetime.now(UTC))
    assert pruned == {"weird": "not-a-date"}


# --- Idempotency: already-commented permalink check (spec §5.2 step 5f/§9.6) --


def test_permalink_already_commented_true_when_marker_and_permalink_present() -> None:
    gh = FakeGitHubClient(
        comments=[
            "hello",
            f"{STATUS_CONFIRMATION_MARKER}\n\nSee https://mail.google.com/mail/u/0/#all/x",
        ]
    )
    assert gmail_sync._permalink_already_commented(
        gh, 1, "https://mail.google.com/mail/u/0/#all/x", STATUS_CONFIRMATION_MARKER
    )


def test_permalink_already_commented_false_when_permalink_absent() -> None:
    gh = FakeGitHubClient(comments=[f"{STATUS_CONFIRMATION_MARKER}\n\nhello there"])
    assert not gmail_sync._permalink_already_commented(
        gh, 1, "https://mail.google.com/mail/u/0/#all/x", STATUS_CONFIRMATION_MARKER
    )


def test_permalink_already_commented_false_when_marker_mismatched() -> None:
    """A different marker's comment referencing the same permalink does NOT
    count as "already handled" for this marker (regression test for the
    heads-up-vs-confirmation conflation bug this signature closes)."""
    gh = FakeGitHubClient(
        comments=[
            f"{GMAIL_NOTICE_MARKER}\n\nSee https://mail.google.com/mail/u/0/#all/x"
        ]
    )
    assert not gmail_sync._permalink_already_commented(
        gh, 1, "https://mail.google.com/mail/u/0/#all/x", STATUS_CONFIRMATION_MARKER
    )


# --- _redact_gmail_secrets (spec §9.5) ----------------------------------------


def test_redact_gmail_secrets_scrubs_each_value() -> None:
    text = gmail_sync._redact_gmail_secrets(
        "id=abc secret=xyz token=123", "abc", "xyz", "123"
    )
    assert text == "id=[REDACTED] secret=[REDACTED] token=[REDACTED]"


def test_redact_gmail_secrets_handles_substring_regardless_of_call_order() -> None:
    """Regression test: redacting a shorter secret before a longer one that
    contains it as a substring must not leave a mangled partial remainder of
    the longer secret in the output."""
    text = gmail_sync._redact_gmail_secrets("leaked: xyzabcdef", "abc", "xyzabcdef")
    assert text == "leaked: [REDACTED]"
    assert "abc" not in text


def test_redact_gmail_secrets_ignores_empty_values() -> None:
    text = gmail_sync._redact_gmail_secrets("id=abc", "abc", "", None)  # type: ignore[arg-type]
    assert text == "id=[REDACTED]"


# --- process_message: DMARC gate (spec §5.1.2, §9.2) -------------------------


def test_process_message_dmarc_failure_makes_zero_llm_calls() -> None:
    """A DMARC-failing message never reaches match_email_to_candidate."""
    processed: dict[str, str] = {}
    gmail_client = FakeGmailClient(messages={"msg-1": make_message(authentic=False)})
    gh = FakeGmailGitHubClient()

    with patch("jobgitops.cli.gmail_sync.match_email_to_candidate") as mocked_match:
        gmail_sync.process_message(
            message_id="msg-1",
            gmail_client=gmail_client,
            gh_client=gh,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path("/tmp/unused"),
            candidate_pool=[make_candidate(1)],
            processed=processed,
        )

    mocked_match.assert_not_called()
    assert "msg-1" in processed
    assert gh.posted_comments == []


def test_process_message_deleted_between_list_and_fetch_marks_processed() -> None:
    """A message deleted before fetch (404) is marked processed, not retried."""

    class NotFoundGmailClient(FakeGmailClient):
        def get_message(self, message_id: str) -> Message:
            raise GmailMessageNotFoundError("gone")

    processed: dict[str, str] = {}
    gmail_sync.process_message(
        message_id="msg-1",
        gmail_client=NotFoundGmailClient(),
        gh_client=FakeGmailGitHubClient(),
        llm_client=MagicMock(),
        settings=sample_settings(),
        resume=sample_resume(),
        repo_path=Path("/tmp/unused"),
        candidate_pool=[],
        processed=processed,
    )
    assert "msg-1" in processed


# --- process_message: pre-filter tier outcomes (spec §6.1/§6.2) -------------


def _run_process_message(
    *,
    gh: FakeGmailGitHubClient,
    candidate_pool: list[Candidate],
    prefilter_result: PreFilterResult,
    match_result: EmailMatchResult,
    processed: dict[str, str] | None = None,
) -> tuple[dict[str, str], MagicMock]:
    """Run `process_message` once with the pre-filter/LLM match mocked out.

    Returns `(processed, match_mock)` so callers can additionally assert on
    `match_mock.call_args` -- e.g. that the candidate list actually forwarded
    to `match_email_to_candidate` is the pre-filter-narrowed one (spec §10).
    """
    processed = processed if processed is not None else {}
    gmail_client = FakeGmailClient(messages={"msg-1": make_message()})
    with (
        patch(
            "jobgitops.cli.gmail_sync.prefilter_candidates",
            return_value=prefilter_result,
        ),
        patch(
            "jobgitops.cli.gmail_sync.match_email_to_candidate",
            return_value=match_result,
        ) as match_mock,
    ):
        gmail_sync.process_message(
            message_id="msg-1",
            gmail_client=gmail_client,
            gh_client=gh,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path("/tmp/unused"),
            candidate_pool=candidate_pool,
            processed=processed,
        )
    return processed, match_mock


@pytest.mark.parametrize("tier", ["zero_hit", "one_hit", "multi_hit"])
def test_process_message_status_null_is_quiet_skip_at_every_tier(tier: str) -> None:
    """status: null is always a quiet skip, regardless of tier (spec §6.2 row 1)."""
    candidate = make_candidate(1)
    gh = FakeGmailGitHubClient(issues=[gh_issue(1)])
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier=tier, candidates=[candidate]),
        match_result=EmailMatchResult(issue_number=None, status=None, summary=""),
    )
    assert gh.posted_comments == []
    assert gh.closed_issues == []
    assert "msg-1" in processed


def test_process_message_zero_hit_null_issue_is_quiet_skip_even_large_pool() -> None:
    """Zero-hit tier, issue_number null: quiet skip -- NEVER a heads-up, even
    with a large full candidate pool (spec §5.2 step 5d regression test)."""
    large_pool = [make_candidate(n) for n in range(1, 51)]
    gh = FakeGmailGitHubClient(issues=[gh_issue(n) for n in range(1, 51)])
    processed, match_mock = _run_process_message(
        gh=gh,
        candidate_pool=large_pool,
        prefilter_result=PreFilterResult(tier="zero_hit", candidates=large_pool),
        match_result=EmailMatchResult(
            issue_number=None, status="applied", summary="Applied somewhere"
        ),
    )
    assert gh.posted_comments == []
    assert "msg-1" in processed
    # The zero-hit tier passes the FULL pool to the LLM call -- proves the
    # candidate list actually forwarded matches the pre-filter's tier output
    # (spec §10), not just that the outcome-handling branch is right.
    forwarded_candidates = match_mock.call_args.args[4]
    assert {c["number"] for c in forwarded_candidates} == {c.number for c in large_pool}


def test_process_message_one_hit_null_issue_is_quiet_skip() -> None:
    """One-hit tier, model rejects the pre-filter's single guess: quiet skip."""
    candidate = make_candidate(1)
    other_candidate = make_candidate(2)
    gh = FakeGmailGitHubClient(issues=[gh_issue(1)])
    processed, match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate, other_candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(
            issue_number=None, status="applied", summary="Applied somewhere"
        ),
    )
    assert gh.posted_comments == []
    assert "msg-1" in processed
    # Only the pre-filter's single narrowed candidate is forwarded, not the
    # full two-candidate pool (spec §10).
    forwarded_candidates = match_mock.call_args.args[4]
    assert [c["number"] for c in forwarded_candidates] == [1]


def test_process_message_multi_hit_null_issue_posts_heads_up_on_each_candidate() -> (
    None
):
    """Multi-hit tier, model can't resolve: heads-up comment on each narrowed
    candidate, marked with GMAIL_NOTICE_MARKER, not STATUS_CONFIRMATION_MARKER."""
    candidates = [make_candidate(1), make_candidate(2)]
    other_candidate = make_candidate(3)
    gh = FakeGmailGitHubClient(issues=[gh_issue(1), gh_issue(2)])
    processed, match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[*candidates, other_candidate],
        prefilter_result=PreFilterResult(tier="multi_hit", candidates=candidates),
        match_result=EmailMatchResult(
            issue_number=None, status="applied", summary="Applied somewhere"
        ),
    )
    assert len(gh.posted_comments) == 2
    posted_issue_numbers = {issue_number for issue_number, _ in gh.posted_comments}
    assert posted_issue_numbers == {1, 2}
    for _, body in gh.posted_comments:
        assert body.startswith(GMAIL_NOTICE_MARKER)
        assert STATUS_CONFIRMATION_MARKER not in body
    assert "msg-1" in processed
    # Only the narrowed multi-hit candidates are forwarded, never the third,
    # unmatched pool member (spec §10).
    forwarded_candidates = match_mock.call_args.args[4]
    assert {c["number"] for c in forwarded_candidates} == {1, 2}


def test_process_message_multi_hit_heads_up_skips_already_commented_candidate() -> None:
    """A candidate that already has this message's heads-up comment is not
    re-commented (idempotency, spec §9.6)."""
    candidates = [make_candidate(1), make_candidate(2)]
    permalink = gmail_sync.build_gmail_permalink("msg-1")
    gh = FakeGmailGitHubClient(
        issues=[gh_issue(1), gh_issue(2)],
        comments=[f"{GMAIL_NOTICE_MARKER}\n\nSee {permalink}"],
    )
    # FakeGitHubClient.list_comments returns the same comment list for every
    # issue number (it's not per-issue), so both candidates see it here --
    # this still proves the dedup check runs before each post.
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=candidates,
        prefilter_result=PreFilterResult(tier="multi_hit", candidates=candidates),
        match_result=EmailMatchResult(
            issue_number=None, status="applied", summary="Applied somewhere"
        ),
    )
    assert gh.posted_comments == []
    assert "msg-1" in processed


def test_process_message_multi_hit_heads_up_does_not_suppress_later_confirmation() -> (
    None
):
    """A prior heads-up comment referencing this permalink must NOT suppress
    a later, confidently-resolved confirmation for the same candidate
    (regression test: `_permalink_already_commented` must be marker-aware,
    not permalink-only, spec §9.6)."""
    candidate = make_candidate(1)
    permalink = gmail_sync.build_gmail_permalink("msg-1")
    # Starting label deliberately differs from the resolved status's target
    # label ("applied"), so `sync_lifecycle_label`'s own already-correct
    # no-op doesn't mask whether `execute_action` actually ran.
    gh = FakeGmailGitHubClient(
        issues=[gh_issue(1, node_id="ND_1", labels=["ready-to-apply"])],
        comments=[f"{GMAIL_NOTICE_MARKER}\n\nSee {permalink}"],
    )
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(
            issue_number=1, status="applied", summary="Applied"
        ),
    )
    assert gh.added_labels == [(1, ["applied"])]
    assert any(
        body.startswith(STATUS_CONFIRMATION_MARKER) for _, body in gh.posted_comments
    )
    assert "msg-1" in processed


@pytest.mark.parametrize(("status", "expected_label"), sorted(STATUS_LABELS.items()))
def test_process_message_resolved_match_executes_status_update(
    status: str, expected_label: str
) -> None:
    """A resolved issue_number + status, for every VALID_STATUS, executes
    through execute_action -- label sync, confirmation comment, Projects V2
    (spec §10, mirroring test_respond.py::test_execute_action_status_update)."""
    candidate = make_candidate(42, title="Staff Engineer at Acme")
    # ready-to-apply never collides with any VALID_STATUSES target label, so
    # `sync_lifecycle_label`'s already-correct no-op never masks whether
    # execute_action actually ran, for any of the four parametrized statuses.
    gh = FakeGmailGitHubClient(
        issues=[gh_issue(42, node_id="ND_42", labels=["ready-to-apply"])]
    )
    processed, match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(
            issue_number=42, status=status, summary="Status update"
        ),
    )
    assert gh.added_labels == [(42, [expected_label])]
    assert len(gh.posted_comments) == 1
    issue_number, body = gh.posted_comments[0]
    assert issue_number == 42
    assert body.startswith(STATUS_CONFIRMATION_MARKER)
    assert "Status update" in body
    assert gmail_sync.build_gmail_permalink("msg-1") in body
    assert "msg-1" in processed
    # Exactly this one candidate (one-hit tier) was forwarded to the LLM call.
    forwarded_candidates = match_mock.call_args.args[4]
    assert [c["number"] for c in forwarded_candidates] == [42]


def test_process_message_resolved_terminal_status_closes_issue_end_to_end() -> None:
    """A resolved terminal status (rejected) auto-closes the GitHub issue
    end-to-end through gmail_sync.py -> respond.execute_action, not just
    unit-tested in respond.py isolation (spec §10)."""
    candidate = make_candidate(7, title="Backend Engineer at Acme")
    gh = FakeGmailGitHubClient(issues=[gh_issue(7, node_id="ND_7", labels=["in-loop"])])
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(
            issue_number=7, status="rejected", summary="Not moving forward"
        ),
    )
    assert gh.closed_issues == [7]
    assert gh.added_labels == [(7, ["rejected"])]
    assert "msg-1" in processed


def test_process_message_resolved_match_already_commented_skips_execute_action() -> (
    None
):
    """A resolved match whose confirmation comment already exists (by Gmail
    permalink) is not re-posted/re-executed (spec §5.2 step 5f, §9.6)."""
    candidate = make_candidate(42)
    permalink = gmail_sync.build_gmail_permalink("msg-1")
    gh = FakeGmailGitHubClient(
        issues=[gh_issue(42)],
        comments=[f"{STATUS_CONFIRMATION_MARKER}\n\nAlready handled. {permalink}"],
    )
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(
            issue_number=42, status="applied", summary="Applied"
        ),
    )
    assert gh.posted_comments == []
    assert gh.added_labels == []
    assert "msg-1" in processed


def test_process_message_llm_validation_error_leaves_message_unprocessed() -> None:
    """A malformed/failed LLM match is logged and left unprocessed (retried
    next run, spec §9.6) rather than aborting the whole batch."""
    processed: dict[str, str] = {}
    gmail_client = FakeGmailClient(messages={"msg-1": make_message()})
    with (
        patch(
            "jobgitops.cli.gmail_sync.prefilter_candidates",
            return_value=PreFilterResult(tier="zero_hit", candidates=[]),
        ),
        patch(
            "jobgitops.cli.gmail_sync.match_email_to_candidate",
            side_effect=ValidationError("bad json"),
        ),
    ):
        gmail_sync.process_message(
            message_id="msg-1",
            gmail_client=gmail_client,
            gh_client=FakeGmailGitHubClient(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path("/tmp/unused"),
            candidate_pool=[],
            processed=processed,
        )
    assert processed == {}


def test_process_message_quota_exceeded_propagates() -> None:
    """A QuotaExceededError from the LLM call is NOT swallowed as a
    per-message failure -- it must propagate so main() can exit 75."""
    gmail_client = FakeGmailClient(messages={"msg-1": make_message()})
    with (
        patch(
            "jobgitops.cli.gmail_sync.prefilter_candidates",
            return_value=PreFilterResult(tier="zero_hit", candidates=[]),
        ),
        patch(
            "jobgitops.cli.gmail_sync.match_email_to_candidate",
            side_effect=QuotaExceededError("quota"),
        ),
        pytest.raises(QuotaExceededError),
    ):
        gmail_sync.process_message(
            message_id="msg-1",
            gmail_client=gmail_client,
            gh_client=FakeGmailGitHubClient(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path("/tmp/unused"),
            candidate_pool=[],
            processed={},
        )


# --- Cursor branch git mechanics (spec §5.2 step 7) --------------------------


def test_checkout_state_branch_fetches_before_checkout(tmp_path: Path) -> None:
    """`git fetch` runs strictly before `git checkout` for the state branch,
    and the checkout always resets the local branch to `origin/<branch>`
    (`-B`), rather than reusing a possibly-stale local ref."""
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        return ""

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        gmail_sync._checkout_state_branch(tmp_path)

    fetch_index = next(i for i, c in enumerate(calls) if c[0] == "fetch")
    checkout_index = next(i for i, c in enumerate(calls) if c[0] == "checkout")
    assert fetch_index < checkout_index
    assert calls[fetch_index] == ["fetch", "origin", gmail_sync.STATE_BRANCH]
    assert calls[checkout_index] == [
        "checkout",
        "-B",
        gmail_sync.STATE_BRANCH,
        f"origin/{gmail_sync.STATE_BRANCH}",
        "--",
    ]


def test_checkout_state_branch_creates_orphan_on_first_run(tmp_path: Path) -> None:
    """When the branch doesn't exist on origin, it's created as an orphan
    branch (after the fetch still runs first and fails)."""
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args[0] == "fetch":
            raise GitOpsError("couldn't find remote ref")
        return ""

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        gmail_sync._checkout_state_branch(tmp_path)

    assert calls[0] == ["fetch", "origin", gmail_sync.STATE_BRANCH]
    assert ["checkout", "--orphan", gmail_sync.STATE_BRANCH, "--"] in calls
    assert ["rm", "-rf", "--", "."] in calls


# --- load_cursor: read via git show, never an in-place checkout -------------


def test_load_cursor_returns_empty_when_branch_absent(tmp_path: Path) -> None:
    with patch(
        "jobgitops.cli.gmail_sync.run_git", side_effect=GitOpsError("no such branch")
    ):
        cursor = gmail_sync.load_cursor(tmp_path)
    assert cursor == {"processed": {}, "last_synced_at": None}


def test_load_cursor_parses_existing_state(tmp_path: Path) -> None:
    raw_json = (
        '{"processed": {"m1": "2026-09-19T10:00:00Z"}, '
        '"last_synced_at": "2026-09-19T14:00:00Z"}'
    )

    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args[0] == "fetch":
            return ""
        if args[0] == "show":
            return raw_json
        raise AssertionError(f"unexpected git call: {args}")

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        cursor = gmail_sync.load_cursor(tmp_path)

    assert cursor == {
        "processed": {"m1": "2026-09-19T10:00:00Z"},
        "last_synced_at": "2026-09-19T14:00:00Z",
    }


def test_load_cursor_does_not_checkout(tmp_path: Path) -> None:
    """load_cursor never issues a `checkout` -- only fetch + show."""
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args[0] == "show":
            return '{"processed": {}, "last_synced_at": null}'
        return ""

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        gmail_sync.load_cursor(tmp_path)

    assert not any(c[0] == "checkout" for c in calls)


# --- Push retry-once-then-give-up (spec §5.2 step 7) -------------------------


def test_push_state_branch_succeeds_on_first_try(tmp_path: Path) -> None:
    with patch("jobgitops.cli.gmail_sync.run_git", return_value=""):
        assert gmail_sync._push_state_branch(tmp_path) is True


def test_push_state_branch_retries_once_then_succeeds(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args[0] == "push" and calls.count(args) == 1:
            raise GitOpsError("rejected: non-fast-forward")
        return ""

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        result = gmail_sync._push_state_branch(tmp_path)

    assert result is True
    push_calls = [c for c in calls if c[0] == "push"]
    assert len(push_calls) == 2
    assert any(c[0] == "fetch" for c in calls)
    assert any(c[0] == "rebase" for c in calls)


def test_push_state_branch_gives_up_after_one_retry(tmp_path: Path) -> None:
    """Both the initial push and the single retry fail: give up (return
    False), without raising -- side effects already applied must not be
    rolled back."""
    with patch(
        "jobgitops.cli.gmail_sync.run_git",
        side_effect=GitOpsError("still rejected"),
    ):
        result = gmail_sync._push_state_branch(tmp_path)
    assert result is False


def test_finalize_cursor_push_failure_does_not_raise(tmp_path: Path) -> None:
    """A total push failure in _finalize_cursor is logged, not raised."""
    with (
        patch("jobgitops.cli.gmail_sync.run_git", return_value="deadbeef"),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch"),
        patch("jobgitops.cli.gmail_sync._commit_cursor", return_value=True),
        patch("jobgitops.cli.gmail_sync._push_state_branch", return_value=False),
    ):
        # Must not raise.
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})


def test_finalize_cursor_restores_original_ref_after_state_branch_work(
    tmp_path: Path,
) -> None:
    """After committing the cursor onto the orphan gmail-sync-state branch,
    _finalize_cursor must check the working tree back out to the ref it
    started on -- later `gmail-sync.yml` steps (badge updates) run against
    this same checkout and need files that only exist on the original
    branch, not on the disconnected orphan branch."""
    git_calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        git_calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch"),
        patch("jobgitops.cli.gmail_sync._commit_cursor", return_value=True),
        patch("jobgitops.cli.gmail_sync._push_state_branch", return_value=True),
    ):
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})

    assert git_calls[0] == ["rev-parse", "HEAD"]
    assert git_calls[-1] == ["checkout", "--force", "abc123", "--"]


def test_finalize_cursor_restores_original_ref_even_on_checkout_failure(
    tmp_path: Path,
) -> None:
    """The original-ref restore must run even when checking out the state
    branch itself fails, since the failure path still logs-not-raises and
    later workflow steps still need the original branch back."""
    git_calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        git_calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch(
            "jobgitops.cli.gmail_sync._checkout_state_branch",
            side_effect=GitOpsError("boom"),
        ),
    ):
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})

    assert git_calls == [
        ["rev-parse", "HEAD"],
        ["checkout", "--force", "abc123", "--"],
    ]


def test_finalize_cursor_logs_restore_failure_without_raising(
    tmp_path: Path,
) -> None:
    """If the final restore checkout itself fails (e.g. the ref no longer
    exists locally), that must be logged, not raised -- already-applied
    GitHub issue side effects must never be rolled back for a working-tree
    housekeeping problem."""

    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        if args[0] == "checkout" and "--force" in args:
            raise GitOpsError("ref no longer exists")
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch"),
        patch("jobgitops.cli.gmail_sync._commit_cursor", return_value=True),
        patch("jobgitops.cli.gmail_sync._push_state_branch", return_value=True),
    ):
        # Must not raise.
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})


# --- Full run_sync() orchestration (per-run cap, cursor writeback) ----------


def _run_full_sync(
    tmp_path: Path,
    *,
    message_ids: list[str],
    gh: FakeGmailGitHubClient | None = None,
    gmail_client: FakeGmailClient | None = None,
) -> tuple[dict, list[list[str]]]:
    """Run a full `run_sync` pass with git operations faked, returning the
    written cursor JSON and every `run_git` call made (in order)."""
    messages = {
        message_id: make_message(message_id=message_id, authentic=False)
        for message_id in message_ids
    }
    gmail_client = gmail_client or FakeGmailClient(
        message_ids=message_ids, messages=messages
    )
    gh = gh or FakeGmailGitHubClient()

    git_calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        git_calls.append(args)
        if args[0] == "show":
            raise GitOpsError("no cursor file yet")
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch("jobgitops.cli.gmail_sync.get_candidate_pool", return_value=[]),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),
    ):
        gmail_sync.run_sync(
            repo_path=tmp_path,
            gmail_config=GmailConfig(enabled=True, label="JobGitOps", days_back=7),
            gh_client=gh,
            gmail_client=gmail_client,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
        )

    state_file = tmp_path / gmail_sync.STATE_FILE_REL_PATH
    assert state_file.exists()
    import json as _json

    return _json.loads(state_file.read_text()), git_calls


def test_run_sync_respects_per_run_cap_and_holds_last_synced_at_back(
    tmp_path: Path,
) -> None:
    """A full run_sync pass with a backlog bigger than MAX_MESSAGES_PER_RUN:
    only the cap's worth are processed, and `last_synced_at` is deliberately
    NOT advanced to "now" -- the run didn't actually catch up, so the
    staleness warning must keep firing next run until it does (regression
    test for the cap silently resetting the staleness clock)."""
    many_ids = [f"m{i}" for i in range(gmail_sync.MAX_MESSAGES_PER_RUN + 5)]
    cursor, git_calls = _run_full_sync(tmp_path, message_ids=many_ids)

    assert len(cursor["processed"]) == gmail_sync.MAX_MESSAGES_PER_RUN
    assert cursor["last_synced_at"] is None
    # A commit was still attempted for the (partially) advanced cursor.
    assert ["commit", "-m", gmail_sync.COMMIT_MESSAGE] in git_calls


def test_run_sync_advances_last_synced_at_when_caught_up(tmp_path: Path) -> None:
    """When every eligible message fits within the per-run cap, the run has
    genuinely caught up, so `last_synced_at` IS advanced to "now"."""
    few_ids = ["m0", "m1", "m2"]
    cursor, _git_calls = _run_full_sync(tmp_path, message_ids=few_ids)

    assert len(cursor["processed"]) == len(few_ids)
    assert cursor["last_synced_at"]


def test_run_sync_resolves_label_and_lists_messages_with_configured_params(
    tmp_path: Path,
) -> None:
    """The configured label/query/days_back actually reach the Gmail client
    calls, not just some hardcoded default."""
    gmail_client = FakeGmailClient(message_ids=[], messages={}, label_id="Label_42")
    gh = FakeGmailGitHubClient()

    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args[0] == "show":
            raise GitOpsError("no cursor file yet")
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch("jobgitops.cli.gmail_sync.get_candidate_pool", return_value=[]),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),
    ):
        gmail_sync.run_sync(
            repo_path=tmp_path,
            gmail_config=GmailConfig(
                enabled=True,
                label="JobGitOps",
                query="from:ats.example.com",
                days_back=5,
            ),
            gh_client=gh,
            gmail_client=gmail_client,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
        )

    assert gmail_client.resolve_calls == ["JobGitOps"]
    assert gmail_client.list_calls == [("Label_42", "from:ats.example.com", 5)]


def test_run_sync_already_processed_message_has_zero_side_effects(
    tmp_path: Path,
) -> None:
    """A message ID already in the loaded cursor's `processed` map is
    skipped entirely on this run -- no Gmail fetch, no GitHub side effects
    (spec §10's full-pipeline idempotency requirement, not just the pure
    `_select_batch` filter in isolation)."""
    import json as _json

    class TrackingGmailClient(FakeGmailClient):
        def get_message(self, message_id: str):  # type: ignore[override]
            raise AssertionError(
                f"get_message({message_id!r}) should never be called for an "
                "already-processed message"
            )

    gmail_client = TrackingGmailClient(message_ids=["already-done"])
    gh = FakeGmailGitHubClient()

    existing_cursor = _json.dumps(
        {
            "processed": {"already-done": "2026-09-19T10:00:00Z"},
            "last_synced_at": "2026-09-19T14:00:00Z",
        }
    )

    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args[0] == "show":
            return existing_cursor
        return ""

    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git),
        patch("jobgitops.cli.gmail_sync.get_candidate_pool", return_value=[]),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),
    ):
        gmail_sync.run_sync(
            repo_path=tmp_path,
            gmail_config=GmailConfig(enabled=True, label="JobGitOps", days_back=7),
            gh_client=gh,
            gmail_client=gmail_client,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
        )

    assert gh.posted_comments == []
    assert gh.added_labels == []


# --- Additional coverage: parsing edge cases, defensive branches -----------


def test_normalize_message_date_falls_back_on_malformed_header() -> None:
    """A malformed Date header falls back to 'now' rather than raising."""
    result = gmail_sync._normalize_message_date("not a real date header")
    # Must still be a parseable ISO timestamp (proves the fallback ran).
    assert gmail_sync._parse_iso(result) is not None


def test_normalize_message_date_falls_back_on_empty_header() -> None:
    result = gmail_sync._normalize_message_date("")
    assert gmail_sync._parse_iso(result) is not None


def test_normalize_message_date_assumes_utc_for_naive_parsed_date() -> None:
    """A syntactically valid but timezone-less Date header is treated as UTC
    rather than crashing on the naive/aware datetime mismatch."""
    result = gmail_sync._normalize_message_date("Fri, 19 Sep 2026 10:00:00")
    assert result == "2026-09-19T10:00:00Z"


def test_load_cursor_handles_malformed_json(tmp_path: Path) -> None:
    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args[0] == "fetch":
            return ""
        if args[0] == "show":
            return "{not valid json"
        raise AssertionError(f"unexpected git call: {args}")

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        cursor = gmail_sync.load_cursor(tmp_path)
    assert cursor == {"processed": {}, "last_synced_at": None}


def test_load_cursor_defaults_processed_when_not_a_dict(tmp_path: Path) -> None:
    def fake_run_git(args: list[str], cwd: Path) -> str:
        if args[0] == "fetch":
            return ""
        if args[0] == "show":
            return '{"processed": "not-a-dict", "last_synced_at": null}'
        raise AssertionError(f"unexpected git call: {args}")

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        cursor = gmail_sync.load_cursor(tmp_path)
    assert cursor == {"processed": {}, "last_synced_at": None}


def test_process_message_resolved_issue_not_in_pool_is_defensive_quiet_skip() -> None:
    """Defensive fallback: an issue_number somehow outside the candidate
    pool is a quiet skip, never a crash (should be unreachable given llm.py's
    own allowlist discipline, but this is the safety net)."""
    candidate = make_candidate(1)
    gh = FakeGmailGitHubClient(issues=[gh_issue(1)])
    processed, _match_mock = _run_process_message(
        gh=gh,
        candidate_pool=[candidate],
        prefilter_result=PreFilterResult(tier="one_hit", candidates=[candidate]),
        match_result=EmailMatchResult(issue_number=999, status="applied", summary="x"),
    )
    assert gh.posted_comments == []
    assert gh.added_labels == []
    assert "msg-1" in processed


def test_ensure_git_identity_sets_when_unset(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        # The read-only "get" form is exactly `["config", "user.name"|"user.email"]`;
        # simulate neither being configured yet.
        if len(args) == 2 and args[0] == "config":
            raise GitOpsError("not set")
        return ""

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        gmail_sync._ensure_git_identity(tmp_path)

    assert ["config", "user.name", "github-actions[bot]"] in calls
    assert [
        "config",
        "user.email",
        "github-actions[bot]@users.noreply.github.com",
    ] in calls


def test_ensure_git_identity_leaves_existing_identity_alone(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        return "Existing Name"

    with patch("jobgitops.cli.gmail_sync.run_git", side_effect=fake_run_git):
        gmail_sync._ensure_git_identity(tmp_path)

    assert all(len(c) == 2 for c in calls)  # only the read-only "config x" calls


def test_commit_cursor_returns_false_when_nothing_staged(tmp_path: Path) -> None:
    with (
        patch("jobgitops.cli.gmail_sync.run_git", return_value=""),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        assert gmail_sync._commit_cursor(tmp_path) is False


def test_finalize_cursor_checkout_failure_is_logged_not_raised(tmp_path: Path) -> None:
    with (
        patch("jobgitops.cli.gmail_sync.run_git", return_value="deadbeef"),
        patch(
            "jobgitops.cli.gmail_sync._checkout_state_branch",
            side_effect=GitOpsError("boom"),
        ),
    ):
        # Must not raise.
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})


def test_finalize_cursor_write_failure_is_logged_not_raised(tmp_path: Path) -> None:
    """A write/commit failure (disk full, hook rejection, lock contention) is
    logged, not raised -- consistent containment with the checkout/push
    failure paths, so the caller's already-applied issue side effects are
    never at risk from a cursor-write problem either."""
    with (
        patch("jobgitops.cli.gmail_sync.run_git", return_value="deadbeef"),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch"),
        patch(
            "jobgitops.cli.gmail_sync._write_cursor_file",
            side_effect=OSError("disk full"),
        ),
        patch("jobgitops.cli.gmail_sync._push_state_branch") as mocked_push,
    ):
        # Must not raise.
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})
    mocked_push.assert_not_called()


def test_finalize_cursor_no_changes_skips_push(tmp_path: Path) -> None:
    with (
        patch("jobgitops.cli.gmail_sync.run_git", return_value="deadbeef"),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch"),
        patch(
            "jobgitops.cli.gmail_sync._commit_cursor", return_value=False
        ) as mocked_commit,
        patch("jobgitops.cli.gmail_sync._push_state_branch") as mocked_push,
    ):
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})
    mocked_commit.assert_called_once()
    mocked_push.assert_not_called()


def test_finalize_cursor_rev_parse_failure_skips_state_branch_entirely(
    tmp_path: Path,
) -> None:
    """If HEAD can't even be resolved, _finalize_cursor must not touch the
    state branch at all -- checking it out with no way to restore the
    original ref afterward would strand the working tree."""
    with (
        patch(
            "jobgitops.cli.gmail_sync.run_git",
            side_effect=GitOpsError("not a git repo"),
        ),
        patch("jobgitops.cli.gmail_sync._checkout_state_branch") as mocked_checkout,
    ):
        # Must not raise.
        gmail_sync._finalize_cursor(tmp_path, {"processed": {}, "last_synced_at": "x"})
    mocked_checkout.assert_not_called()


# --- main(): remaining client-init failure branches --------------------------


def test_main_exits_1_when_github_client_init_fails(tmp_path: Path) -> None:
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            side_effect=RuntimeError("bad token"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_gmail_client_init_fails(tmp_path: Path) -> None:
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            return_value=FakeGmailGitHubClient(),
        ),
        patch(
            "jobgitops.cli.gmail_sync.GmailClient",
            side_effect=RuntimeError("bad creds"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_llm_client_init_fails(tmp_path: Path) -> None:
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            return_value=FakeGmailGitHubClient(),
        ),
        patch("jobgitops.cli.gmail_sync.GmailClient", return_value=FakeGmailClient()),
        patch(
            "jobgitops.cli.gmail_sync.get_llm_client",
            side_effect=RuntimeError("no key"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_when_resume_fails_to_load(tmp_path: Path) -> None:
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch(
            "jobgitops.cli.gmail_sync.load_resume",
            side_effect=FileNotFoundError("no resume"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_main_exits_1_on_unexpected_run_sync_failure(tmp_path: Path) -> None:
    settings = gmail_settings()
    with (
        patch.dict(os.environ, GMAIL_ENV, clear=True),
        patch("sys.argv", ["gmail_sync.py", "--repo-path", str(tmp_path)]),
        patch("jobgitops.cli.gmail_sync.load_settings", return_value=settings),
        patch("jobgitops.cli.gmail_sync.load_resume", return_value=sample_resume()),
        patch(
            "jobgitops.cli.gmail_sync.GitHubClient",
            return_value=FakeGmailGitHubClient(),
        ),
        patch("jobgitops.cli.gmail_sync.GmailClient", return_value=FakeGmailClient()),
        patch("jobgitops.cli.gmail_sync.get_llm_client", return_value=MagicMock()),
        patch(
            "jobgitops.cli.gmail_sync.run_sync", side_effect=RuntimeError("unexpected")
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        gmail_sync.main()
    assert exc_info.value.code == 1


def test_run_sync_raises_fatal_error_when_label_missing(tmp_path: Path) -> None:
    gmail_client = FakeGmailClient(label_id=None)
    with (
        patch("jobgitops.cli.gmail_sync.run_git", side_effect=GitOpsError("no branch")),
        pytest.raises(gmail_sync.GmailSyncFatalError),
    ):
        gmail_sync.run_sync(
            repo_path=tmp_path,
            gmail_config=GmailConfig(enabled=True, label="Nope", days_back=7),
            gh_client=FakeGmailGitHubClient(),
            gmail_client=gmail_client,
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
        )
