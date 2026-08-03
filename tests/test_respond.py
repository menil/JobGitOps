"""Unit tests for the Issue Assistant responder (spec 5.1).

Covers event classification, bot/marker/empty-comment skips, the opened-issue
auto-detect guard, side-effect execution per action (with the Projects V2
column move delegated to ``status-transition.yml``), the fetch-failure path,
and the quota-exit ``75`` behavior.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import respond
import triage
from jobgitops.assistant import (
    STATUS_CONFIRMATION_MARKER,
    STATUS_LABELS,
    AgentAction,
)
from jobgitops.llm import QuotaExceededError
from jobgitops.schema import Resume, Settings

DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}


def sample_resume() -> Resume:
    """Build a minimal resume for the responder's context loading."""
    return Resume.from_dict({"basics": {"name": "Jordan Sample"}})


def sample_settings() -> Settings:
    """Default Settings with no Projects V2 integration (label-only fallback)."""
    return Settings(fit_threshold=3.5)


def comment_event(
    body: str = "Is this company profitable?", action: str = "created"
) -> dict:
    """Build an ``issue_comment`` webhook payload."""
    return {
        "action": action,
        "comment": {
            "id": 1,
            "body": body,
            "user": {"login": "meni", "type": "User"},
        },
        "issue": {
            "number": 5,
            "title": "Senior Python Engineer at Acme",
            "body": "## Job Description\nBuild things.",
            "node_id": "ND_5",
            "labels": [{"name": "ready-to-apply"}],
        },
        "repository": {"full_name": "owner/repo"},
    }


def opened_event(
    body: str = "https://careers.acme.com/jobs/123",
    labels: list[str] | None = None,
    action: str = "opened",
) -> dict:
    """Build an ``issues`` webhook payload."""
    return {
        "action": action,
        "issue": {
            "number": 6,
            "title": "My job submission",
            "body": body,
            "node_id": "ND_6",
            "labels": [{"name": label} for label in (labels or [])],
        },
        "repository": {"full_name": "owner/repo"},
    }


class FakeGitHubClient:
    """In-memory GitHubClient double recording every side effect."""

    def __init__(
        self, labels: list[str] | None = None, comments: list[str] | None = None
    ):
        self.labels = list(labels or [])
        self.comments = list(comments or [])
        self.posted_comments: list[tuple[int, str]] = []
        self.added_labels: list[tuple[int, list[str]]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.project_statuses: list[tuple[str, str]] = []

    def get_labels(self, issue_number: int) -> list[str]:
        return list(self.labels)

    def list_comments(
        self, issue_number: int, per_page: int = 100, page: int | None = None
    ) -> list[dict]:
        return [{"id": i, "body": body} for i, body in enumerate(self.comments)]

    def post_comment(self, issue_number: int, body: str) -> dict:
        self.posted_comments.append((issue_number, body))
        return {"id": len(self.posted_comments)}

    def add_labels(self, issue_number: int, labels: list[str]) -> list[str]:
        self.added_labels.append((issue_number, labels))
        self.labels = sorted(set(self.labels) | set(labels))
        return list(self.labels)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.removed_labels.append((issue_number, label))

    def close_issue(self, issue_number: int) -> dict:
        return {}

    def update_project_status(self, issue_node_id: str, status_name: str) -> None:
        self.project_statuses.append((issue_node_id, status_name))


def write_event(tmp_path: Path, data: dict) -> str:
    """Write a webhook payload to a temp file and return its path."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(data), encoding="utf-8")
    return str(event_path)


# --- Event classification ----------------------------------------------------


def test_classify_event_comment() -> None:
    """An issue_comment created event is classified as the comment flow."""
    assert respond.classify_event(comment_event()) == "comment"


def test_classify_event_opened() -> None:
    """An issues opened event is classified as the opened-issue flow."""
    assert respond.classify_event(opened_event()) == "opened"


@pytest.mark.parametrize(
    ("event", "match"),
    [
        (comment_event(action="deleted"), "Unsupported webhook event"),
        (opened_event(action="edited"), "Unsupported webhook event"),
        ({}, "Unsupported webhook event"),
        ({"action": "opened"}, "Unsupported webhook event"),
    ],
)
def test_classify_event_unsupported(event: dict, match: str) -> None:
    """Unsupported event shapes raise a descriptive RespondError."""
    with pytest.raises(respond.RespondError, match=match):
        respond.classify_event(event)


# --- Bot / marker / empty-comment skips --------------------------------------


def test_is_bot_author_type_bot() -> None:
    """Comments authored by GitHub app/bot accounts are skipped."""
    assert respond.is_bot_author({"login": "github-actions[bot]", "type": "Bot"}, set())


def test_is_bot_author_blocklist_login() -> None:
    """Logins in the AGENT_BOT_LOGINS blocklist are skipped (case-insensitive)."""
    assert respond.is_bot_author({"login": "MyBot", "type": "User"}, {"mybot"})


def test_is_bot_author_human_not_skipped() -> None:
    """Human authors outside the blocklist are never skipped."""
    assert not respond.is_bot_author({"login": "meni", "type": "User"}, set())
    assert not respond.is_bot_author({"login": "meni", "type": "User"}, {"otherbot"})
    assert not respond.is_bot_author(None, set())
    assert not respond.is_bot_author({}, set())


def test_contains_confirmation_marker() -> None:
    """The assistant's own confirmation marker is recognized exactly."""
    assert respond.contains_confirmation_marker(
        f"{STATUS_CONFIRMATION_MARKER}\n\nMarked as applied."
    )
    assert not respond.contains_confirmation_marker("I applied!")
    assert not respond.contains_confirmation_marker(None)


def test_bot_logins_from_env() -> None:
    """AGENT_BOT_LOGINS parses into a lowercased set of logins."""
    with patch.dict(os.environ, {"AGENT_BOT_LOGINS": " BotA ,botb , "}, clear=True):
        assert respond._bot_logins_from_env() == {"bota", "botb"}


# --- handle_comment_event guards ---------------------------------------------


def _run_comment_flow(
    gh: FakeGitHubClient,
    event: dict,
    action: AgentAction | None = None,
    *,
    run_agent_side_effect: Exception | None = None,
) -> list:
    """Drive handle_comment_event with patched run_agent and fakes."""
    with patch(
        "respond.run_agent", return_value=action, side_effect=run_agent_side_effect
    ) as mocked:
        respond.handle_comment_event(
            event,
            gh_client=gh,
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
            bot_logins=set(),
        )
        return mocked


def test_comment_flow_skips_bot_author() -> None:
    """A bot-authored comment never reaches the agent loop."""
    gh = FakeGitHubClient()
    event = comment_event()
    event["comment"]["user"] = {"login": "github-actions[bot]", "type": "Bot"}

    mocked = _run_comment_flow(gh, event, AgentAction(action="reply", reply="x"))
    mocked.assert_not_called()
    assert gh.posted_comments == []


def test_comment_flow_skips_blocklisted_login() -> None:
    """A blocklisted non-bot login never reaches the agent loop."""
    gh = FakeGitHubClient()
    event = comment_event()
    event["comment"]["user"] = {"login": "claude-code", "type": "User"}

    with (
        patch.dict(os.environ, {"AGENT_BOT_LOGINS": "claude-code"}, clear=True),
        patch("respond.run_agent") as mocked,
    ):
        respond.handle_comment_event(
            event,
            gh_client=gh,
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
            bot_logins=respond._bot_logins_from_env(),
        )
        mocked.assert_not_called()


def test_comment_flow_skips_empty_comment() -> None:
    """An empty comment is a no-op."""
    gh = FakeGitHubClient()
    mocked = _run_comment_flow(
        gh, comment_event(body="   "), AgentAction(action="skip")
    )
    mocked.assert_not_called()


def test_comment_flow_skips_confirmation_marker() -> None:
    """A comment carrying the confirmation marker is skipped deterministically."""
    gh = FakeGitHubClient()
    event = comment_event(body=f"{STATUS_CONFIRMATION_MARKER}\n\nMarked applied.")
    mocked = _run_comment_flow(gh, event, AgentAction(action="reply", reply="x"))
    mocked.assert_not_called()
    assert gh.posted_comments == []


def test_comment_flow_missing_issue_number() -> None:
    """A comment event without an issue number is rejected."""
    event = comment_event()
    event.pop("issue")
    with pytest.raises(respond.RespondError, match="no issue number"):
        respond.handle_comment_event(
            event,
            gh_client=FakeGitHubClient(),
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
            bot_logins=set(),
        )


def test_comment_flow_loads_context_and_runs_agent() -> None:
    """The thread context is loaded and the agent runs with the trigger text."""
    gh = FakeGitHubClient(
        labels=["ready-to-apply"], comments=["prior question", "the trigger"]
    )
    mocked = _run_comment_flow(
        gh, comment_event(body="the trigger"), AgentAction(action="skip")
    )

    mocked.assert_called_once()
    kwargs = mocked.call_args.kwargs
    assert kwargs["trigger_text"] == "the trigger"
    assert kwargs["issue_title"] == "Senior Python Engineer at Acme"
    assert kwargs["issue_body"] == "## Job Description\nBuild things."
    assert kwargs["labels"] == ["ready-to-apply"]
    assert kwargs["comments"] == ["prior question", "the trigger"]


# --- Side-effect execution ---------------------------------------------------


def test_execute_action_skip_is_noop() -> None:
    """A skip action performs no GitHub side effects."""
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="skip"),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.posted_comments == []
    assert gh.added_labels == []
    assert gh.project_statuses == []


def test_execute_action_reply_posts_comment() -> None:
    """A reply action posts the markdown reply with no labels or column moves."""
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="reply", reply="  **Acme is private.**  "),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.posted_comments == [(5, "**Acme is private.**")]
    assert gh.added_labels == []
    assert gh.project_statuses == []


def test_execute_action_reply_empty_is_noop() -> None:
    """An empty reply is not posted."""
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="reply", reply="   "),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.posted_comments == []


@pytest.mark.parametrize("status", ["applied", "interviewing", "rejected"])
def test_execute_action_status_update(status: str) -> None:
    """A status_update adds the label and posts a marker-prefixed confirmation.

    The Projects V2 column move is delegated to status-transition.yml: the
    responder must never call update_project_status itself.
    """
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="status_update", status=status, reply="Marked."),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.added_labels == [(5, [STATUS_LABELS[status]])]
    assert len(gh.posted_comments) == 1
    body = gh.posted_comments[0][1]
    assert body.startswith(STATUS_CONFIRMATION_MARKER)
    assert "Marked." in body
    assert gh.project_statuses == []


def test_execute_action_status_update_marker_only_without_reply() -> None:
    """A status_update without a reply still posts the hidden marker."""
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="status_update", status="applied", reply="  "),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.posted_comments == [(5, STATUS_CONFIRMATION_MARKER)]


def test_execute_action_triage_skipped_when_triage_pending() -> None:
    """A triage action is skipped when the issue is labeled triage-pending."""
    gh = FakeGitHubClient(labels=["triage-pending"])
    with patch("respond.triage.run_triage") as mocked_run:
        respond.execute_action(
            AgentAction(action="triage"),
            issue_number=5,
            issue_title="t",
            issue_body="https://careers.acme.com/jobs/123",
            issue_node_id="ND",
            repo_path=Path(),
            gh_client=gh,
            settings=sample_settings(),
            resume=sample_resume(),
            llm_client=MagicMock(),
            web_client=MagicMock(),
        )
        mocked_run.assert_not_called()
    assert gh.posted_comments == []


def test_execute_action_triage_runs_shared_core() -> None:
    """A triage action runs the shared triage core with fresh context."""
    gh = FakeGitHubClient(labels=["ready-to-apply"])
    with patch("respond.triage.run_triage") as mocked_run:
        respond.execute_action(
            AgentAction(action="triage"),
            issue_number=5,
            issue_title="Role at Acme",
            issue_body="https://careers.acme.com/jobs/123",
            issue_node_id="ND",
            repo_path=Path("/repo"),
            gh_client=gh,
            settings=sample_settings(),
            resume=sample_resume(),
            llm_client="llm",
            web_client="web",
        )
    mocked_run.assert_called_once()
    kwargs = mocked_run.call_args.kwargs
    assert kwargs["issue_number"] == 5
    assert kwargs["issue_body"] == "https://careers.acme.com/jobs/123"
    assert kwargs["issue_labels"] == ["ready-to-apply"]
    assert kwargs["repo_path"] == Path("/repo")
    assert kwargs["llm_client"] == "llm"
    assert kwargs["web_client"] == "web"


def test_execute_action_unknown_action_logs_noop() -> None:
    """An unknown action string is ignored rather than acted on."""
    gh = FakeGitHubClient()
    respond.execute_action(
        AgentAction(action="explode"),
        issue_number=5,
        issue_title="t",
        issue_body="b",
        issue_node_id="ND",
        repo_path=Path(),
        gh_client=gh,
        settings=sample_settings(),
        resume=sample_resume(),
        llm_client=MagicMock(),
        web_client=MagicMock(),
    )
    assert gh.posted_comments == []
    assert gh.added_labels == []


# --- Opened-issue auto-detect guard ------------------------------------------


def test_should_auto_detect_bare_url() -> None:
    """A bare job-URL submission with no labels is auto-detected."""
    assert respond.should_auto_detect("https://careers.acme.com/jobs/123", [])


def test_should_auto_detect_no_url() -> None:
    """Bodies without an http(s) URL are never auto-detected."""
    assert not respond.should_auto_detect("please triage this", [])
    assert not respond.should_auto_detect("", [])


def test_should_auto_detect_url_with_trailing_punctuation() -> None:
    """Trailing punctuation is stripped when extracting the URL."""
    assert respond._extract_url("See https://careers.acme.com/jobs/123.") == (
        "https://careers.acme.com/jobs/123"
    )
    assert respond._extract_url("no links here") is None


@pytest.mark.parametrize("label", sorted(respond.JOB_LABELS))
def test_should_auto_detect_rejects_any_job_label(label: str) -> None:
    """Issues carrying any job label are already owned by the pipeline."""
    assert not respond.should_auto_detect("https://careers.acme.com/jobs/123", [label])


def test_respond_workflow_short_circuit_label_in_job_labels() -> None:
    """The respond workflow's short-circuit label stays a known job label.

    The workflow's ``if:`` expression only gates on ``triage-pending`` (the one
    pipeline label present in an ``issues: opened`` event); the full guard lives
    in ``should_auto_detect``. This drift test keeps the YAML's label in sync
    with the authoritative ``JOB_LABELS`` set in ``respond.py``.
    """
    repo_root = Path(__file__).resolve().parents[1]
    workflow_path = repo_root / ".github/workflows/respond-issue.yml"
    assert workflow_path.is_file(), "respond-issue.yml must exist"

    raw = workflow_path.read_text(encoding="utf-8")
    pattern = r"!contains\(github\.event\.issue\.labels\.\*\.name,\s*'([^']+)'\)"
    match = re.search(pattern, raw)
    assert match, "workflow if: must reference a label in its short-circuit"
    short_circuit_label = match.group(1)

    assert short_circuit_label in respond.JOB_LABELS, (
        f"workflow short-circuit label {short_circuit_label!r} "
        f"must be a member of respond.JOB_LABELS"
    )


@pytest.mark.parametrize(
    "structured_body",
    [
        "## Job Description\nBuild things.",
        "**Company:** Acme\n\n## Job Description\nBuild.",
        "**Apply URL:** https://acme.com\n\nDetails",
        "**Role:** Engineer\n\nDescription",
    ],
)
def test_should_auto_detect_rejects_structured_bodies(structured_body: str) -> None:
    """Scraper-created (already structured) bodies are never re-processed."""
    assert not respond.should_auto_detect(structured_body, [])


def test_handle_opened_event_skips_triage_pending() -> None:
    """A triage-pending labeled issue is left to the triage webhook."""
    event = opened_event(labels=["triage-pending"])
    with (
        patch("respond.triage.run_triage") as mocked_run,
        patch("respond.triage.fetch_job_page") as mocked_fetch,
    ):
        respond.handle_opened_event(
            event,
            gh_client=FakeGitHubClient(),
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
        )
        mocked_run.assert_not_called()
        mocked_fetch.assert_not_called()


def test_handle_opened_event_skips_structured_or_labeled() -> None:
    """Scraper issues (structured or labeled) never reach triage."""
    for event in (
        opened_event(body="## Job Description\nFull description."),
        opened_event(labels=["ready-to-apply"]),
        opened_event(body="no url here"),
    ):
        with (
            patch("respond.triage.run_triage") as mocked_run,
            patch("respond.triage.fetch_job_page") as mocked_fetch,
        ):
            respond.handle_opened_event(
                event,
                gh_client=FakeGitHubClient(),
                web_client=MagicMock(),
                llm_client=MagicMock(),
                settings=sample_settings(),
                resume=sample_resume(),
                repo_path=Path(),
            )
            mocked_run.assert_not_called()
            mocked_fetch.assert_not_called()


def test_handle_opened_event_fetch_failure_posts_comment() -> None:
    """A fetch failure posts an explanatory comment and never closes the issue."""
    gh = FakeGitHubClient()
    with (
        patch("respond.triage.run_triage") as mocked_run,
        patch(
            "respond.triage.fetch_job_page",
            side_effect=triage.JobFetchError("blocked by bot"),
        ) as mocked_fetch,
        patch("respond.triage.post_fetch_failure_comment") as mocked_comment,
    ):
        respond.handle_opened_event(
            opened_event(),
            gh_client=gh,
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
        )
        mocked_fetch.assert_called_once()
        mocked_comment.assert_called_once_with(
            6, gh, "https://careers.acme.com/jobs/123", "blocked by bot"
        )
        mocked_run.assert_not_called()


def test_handle_opened_event_runs_triage_with_canonical_body() -> None:
    """The fetched posting is canonicalized and passed to the shared triage core."""
    gh = FakeGitHubClient()
    fetched = {
        "title": "Senior Python Engineer at Acme",
        "description": "Acme hires Python engineers.",
    }
    details = {
        "company": "Acme",
        "role": "Senior Python Engineer",
        "location": "Remote",
        "salary": "Not specified",
        "source": "manual",
        "description": "Acme hires Python engineers.",
    }
    with (
        patch("respond.triage.fetch_job_page", return_value=fetched),
        patch("respond.triage.infer_job_details_from_page", return_value=details),
        patch("respond.triage.run_triage") as mocked_run,
    ):
        respond.handle_opened_event(
            opened_event(),
            gh_client=gh,
            web_client=MagicMock(),
            llm_client="llm",
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path("/repo"),
        )

    mocked_run.assert_called_once()
    kwargs = mocked_run.call_args.kwargs
    assert kwargs["issue_number"] == 6
    assert kwargs["issue_node_id"] == "ND_6"
    assert kwargs["issue_labels"] == []
    assert kwargs["llm_client"] == "llm"
    body = kwargs["issue_body"]
    assert "**Company:** Acme" in body
    assert "**Role:** Senior Python Engineer" in body
    assert "**Source:** manual" in body
    assert "## Job Description" in body
    assert "Acme hires Python engineers." in body


def test_handle_opened_event_missing_issue_number() -> None:
    """An opened event without an issue number is rejected."""
    event = opened_event()
    event.pop("issue")
    with pytest.raises(respond.RespondError, match="no issue number"):
        respond.handle_opened_event(
            event,
            gh_client=FakeGitHubClient(),
            web_client=MagicMock(),
            llm_client=MagicMock(),
            settings=sample_settings(),
            resume=sample_resume(),
            repo_path=Path(),
        )


# --- Diagnostic comment helper -----------------------------------------------


def test_post_diagnostic_comment_posts_body() -> None:
    """An unexpected failure posts a diagnostic comment with the error text."""
    gh = FakeGitHubClient()
    respond._post_diagnostic_comment(gh, opened_event(), RuntimeError("boom"))
    assert len(gh.posted_comments) == 1
    number, body = gh.posted_comments[0]
    assert number == 6
    assert "Issue Assistant: Error" in body
    assert "boom" in body


def test_post_diagnostic_comment_skips_without_issue() -> None:
    """No diagnostic comment is attempted when the event lacks an issue."""
    gh = FakeGitHubClient()
    respond._post_diagnostic_comment(gh, {}, RuntimeError("boom"))
    assert gh.posted_comments == []


def test_post_diagnostic_comment_survives_posting_failure() -> None:
    """A posting failure inside the diagnostic path is logged, not raised."""

    class FailingClient(FakeGitHubClient):
        def post_comment(self, issue_number: int, body: str) -> dict:
            raise RuntimeError("gh down")

    respond._post_diagnostic_comment(
        FailingClient(), opened_event(), RuntimeError("boom")
    )


# --- main() dispatch and exit codes ------------------------------------------


def _run_main(
    tmp_path: Path,
    event: dict,
    env: dict | None = None,
    argv_extra: list[str] | None = None,
):
    """Run main() with a temp event file and assert it exits with ``code``."""
    event_path = write_event(tmp_path, event)
    argv = ["respond.py", "--event-path", event_path, "--repo-path", str(tmp_path)]
    if argv_extra:
        argv.extend(argv_extra)
    with (
        patch.dict(os.environ, DEFAULT_ENV if env is None else env, clear=True),
        patch("sys.argv", argv),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        patch("respond.GitHubClient", return_value=FakeGitHubClient()),
        patch("respond.WebClient", return_value=MagicMock()),
        patch("respond.get_llm_client", return_value=MagicMock()),
        pytest.raises(SystemExit) as exc_info,
    ):
        respond.main()
    return exc_info.value.code


def test_main_comment_flow_happy_path(tmp_path: Path) -> None:
    """A human comment dispatches the agent and posts the reply."""
    gh = FakeGitHubClient()
    event_path = write_event(tmp_path, comment_event(body="Is Acme public?"))
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", event_path, "--repo-path", str(tmp_path)],
        ),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        patch("respond.GitHubClient", return_value=gh),
        patch("respond.WebClient", return_value=MagicMock()),
        patch("respond.get_llm_client", return_value=MagicMock()),
        patch(
            "respond.run_agent",
            return_value=AgentAction(action="reply", reply="Acme is private."),
        ),
    ):
        respond.main()

    assert gh.posted_comments == [(5, "Acme is private.")]


def test_main_opened_event_dispatch(tmp_path: Path) -> None:
    """An opened bare-URL issue is dispatched to the opened-issue handler."""
    event_path = write_event(tmp_path, opened_event())
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", event_path, "--repo-path", str(tmp_path)],
        ),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        patch("respond.GitHubClient", return_value=FakeGitHubClient()),
        patch("respond.WebClient", return_value=MagicMock()),
        patch("respond.get_llm_client", return_value=MagicMock()),
        patch("respond.handle_opened_event") as mocked_opened,
    ):
        respond.main()

    mocked_opened.assert_called_once()
    assert mocked_opened.call_args.args[0]["issue"]["number"] == 6


def test_main_quota_exceeded_exits_75(tmp_path: Path) -> None:
    """A QuotaExceededError exits with code 75 (stop-triage-today convention)."""
    with patch(
        "respond.handle_comment_event",
        side_effect=QuotaExceededError("quota"),
    ) as mocked:
        code = _run_main(tmp_path, comment_event())
    assert code == 75
    mocked.assert_called_once()


def test_main_unexpected_failure_posts_diagnostic_and_exits_1(tmp_path: Path) -> None:
    """Other failures post a diagnostic comment and exit 1."""
    gh = FakeGitHubClient()
    with (
        patch("respond.GitHubClient", return_value=gh),
        patch("respond.handle_comment_event", side_effect=RuntimeError("boom")),
        pytest.raises(SystemExit) as exc_info,
    ):
        event_path = write_event(tmp_path, comment_event())
        with (
            patch.dict(os.environ, DEFAULT_ENV, clear=True),
            patch(
                "sys.argv",
                [
                    "respond.py",
                    "--event-path",
                    event_path,
                    "--repo-path",
                    str(tmp_path),
                ],
            ),
            patch("respond.load_settings", return_value=sample_settings()),
            patch("respond.load_resume", return_value=sample_resume()),
            patch("respond.GitHubClient", return_value=gh),
            patch("respond.WebClient", return_value=MagicMock()),
            patch("respond.get_llm_client", return_value=MagicMock()),
        ):
            respond.main()

    assert exc_info.value.code == 1
    assert any("Issue Assistant: Error" in body for _, body in gh.posted_comments)


def test_main_unsupported_event_exits_0(tmp_path: Path) -> None:
    """An event the responder is not registered for is ignored cleanly."""
    code = _run_main(tmp_path, {"action": "labeled", "label": {"name": "applied"}})
    assert code == 0


def test_main_ignores_event_from_unsupported_action(tmp_path: Path) -> None:
    """A comment event with a non-created action is ignored (no side effects)."""
    gh = FakeGitHubClient()
    with (
        patch("respond.GitHubClient", return_value=gh),
        pytest.raises(SystemExit) as exc_info,
    ):
        event_path = write_event(tmp_path, comment_event(action="deleted"))
        with (
            patch.dict(os.environ, DEFAULT_ENV, clear=True),
            patch(
                "sys.argv",
                [
                    "respond.py",
                    "--event-path",
                    event_path,
                    "--repo-path",
                    str(tmp_path),
                ],
            ),
            patch("respond.load_settings", return_value=sample_settings()),
            patch("respond.load_resume", return_value=sample_resume()),
            patch("respond.GitHubClient", return_value=gh),
            patch("respond.WebClient", return_value=MagicMock()),
            patch("respond.get_llm_client", return_value=MagicMock()),
        ):
            respond.main()
    assert exc_info.value.code == 0
    assert gh.posted_comments == []


def test_main_missing_token_exits_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing GITHUB_TOKEN exits 1 before any client is built."""
    code = _run_main(tmp_path, comment_event(), env={"GITHUB_REPOSITORY": "owner/repo"})
    assert code == 1
    assert "GITHUB_TOKEN environment variable is missing." in caplog.text


def test_main_missing_repository_exits_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing repository exits 1 even when the payload lacks repository info."""
    event = comment_event()
    event.pop("repository")
    code = _run_main(tmp_path, event, env={"GITHUB_TOKEN": "test_token"})
    assert code == 1
    assert "GITHUB_REPOSITORY environment variable is missing." in caplog.text


def test_main_repository_from_event_payload(tmp_path: Path) -> None:
    """GITHUB_REPOSITORY is resolved from the payload when the env var is absent."""
    event_path = write_event(tmp_path, comment_event())
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", event_path, "--repo-path", str(tmp_path)],
        ),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        patch("respond.GitHubClient") as mocked_gh,
        patch("respond.WebClient", return_value=MagicMock()),
        patch("respond.get_llm_client", return_value=MagicMock()),
        patch("respond.handle_comment_event"),
    ):
        respond.main()

    mocked_gh.assert_called_once()
    assert mocked_gh.call_args.kwargs["repo"] == "owner/repo"


def test_main_missing_event_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No event path means nothing to do; exit 1 with a clear message."""
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch("sys.argv", ["respond.py", "--repo-path", str(tmp_path)]),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        pytest.raises(SystemExit) as exc_info,
    ):
        respond.main()
    assert exc_info.value.code == 1
    assert "No event payload specified" in caplog.text


def test_main_invalid_event_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unparseable event JSON exits 1 with a descriptive message."""
    bad_path = tmp_path / "event.json"
    bad_path.write_text("not json", encoding="utf-8")
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", str(bad_path), "--repo-path", str(tmp_path)],
        ),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        pytest.raises(SystemExit) as exc_info,
    ):
        respond.main()
    assert exc_info.value.code == 1
    assert "Failed to parse event payload JSON:" in caplog.text


def test_main_load_failure_exits_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A settings/resume load failure exits 1 before any client is built."""
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", write_event(tmp_path, comment_event())],
        ),
        patch("respond.load_settings", side_effect=RuntimeError("bad settings")),
        pytest.raises(SystemExit) as exc_info,
    ):
        respond.main()
    assert exc_info.value.code == 1
    assert "Failed to load settings or base resume configuration" in caplog.text


def test_main_client_init_failure_exits_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A GitHubClient initialization failure exits 1."""
    event_path = write_event(tmp_path, comment_event())
    with (
        patch.dict(os.environ, DEFAULT_ENV, clear=True),
        patch(
            "sys.argv",
            ["respond.py", "--event-path", event_path, "--repo-path", str(tmp_path)],
        ),
        patch("respond.load_settings", return_value=sample_settings()),
        patch("respond.load_resume", return_value=sample_resume()),
        patch("respond.GitHubClient", side_effect=ValueError("bad repo")),
        pytest.raises(SystemExit) as exc_info,
    ):
        respond.main()
    assert exc_info.value.code == 1
    assert "Failed to initialize GitHubClient: bad repo" in caplog.text
