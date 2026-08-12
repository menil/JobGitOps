"""Unit tests for the Issue Assistant agent loop (spec 5.2)."""

import json

import pytest

import jobgitops.assistant as assistant
from jobgitops.llm import ChatMessage, ToolCall, ValidationError
from jobgitops.schema import ResearchConfig, Resume
from jobgitops.web import PageContent, SearchResult


def sample_resume() -> Resume:
    """Build a minimal resume for prompt-rendering assertions."""
    return Resume.from_dict(
        {
            "basics": {
                "name": "Jordan Sample",
                "email": "jordan@example.com",
                "location": {"city": "Seattle", "region": "WA", "countryCode": "US"},
            },
            "skills": [{"name": "Languages", "keywords": ["Python"]}],
        }
    )


def make_research(**overrides) -> ResearchConfig:
    """Build a ResearchConfig; dataclass defaults already match spec §8.1."""
    return ResearchConfig(**overrides)


class ScriptedLLM:
    """Fake LLMClient that returns a scripted sequence of chat replies."""

    def __init__(self, *responses: ChatMessage) -> None:
        self._responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def chat(
        self, messages: list[ChatMessage], tools: list | None = None
    ) -> ChatMessage:
        self.calls.append(list(messages))
        return self._responses.pop(0)


class StubWebClient:
    """Fake WebClient recording tool invocations for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def web_search(self, query: str) -> list[SearchResult]:
        self.calls.append(("web_search", query))
        return [
            SearchResult(
                title="Acme",
                url="https://acme.com",
                snippet="Acme is a private company.",
            )
        ]

    def fetch_url(self, url: str) -> PageContent:
        self.calls.append(("fetch_url", url))
        return PageContent(
            url=url, title="Acme Careers", text="Acme is hiring.", source="direct"
        )


def run(
    llm: ScriptedLLM,
    *,
    comments: list[str] | None = None,
    trigger: str | None = None,
    research: ResearchConfig | None = None,
    resume: Resume | None = None,
    client: StubWebClient | None = None,
    title: str = "Software Engineer at Acme",
    body: str = "## Job Description\nBuild things.",
    labels: list[str] | None = None,
) -> tuple[assistant.AgentAction, StubWebClient]:
    """Drive run_agent with scripted fakes and return (action, web_client)."""
    client = client or StubWebClient()
    action = assistant.run_agent(
        llm,
        client,
        research or make_research(),
        issue_title=title,
        issue_body=body,
        labels=labels,
        trigger_text=trigger
        if trigger is not None
        else (comments[-1] if comments else body),
        comments=comments or [],
        resume=resume or sample_resume(),
    )
    return action, client


def tool_call_msg(name: str, arguments: dict) -> ChatMessage:
    """Build an assistant turn requesting a single tool call."""
    return ChatMessage(
        role="assistant",
        tool_calls=[ToolCall(name=name, arguments=arguments, id="call_1")],
    )


def action_msg(action: str, **extra) -> ChatMessage:
    """Build an assistant turn holding a final action JSON payload."""
    payload = {"action": action, **extra}
    return ChatMessage(role="assistant", content=json.dumps(payload))


# --- Action parsing ----------------------------------------------------------


def test_parse_action_reply() -> None:
    """A reply action carries its markdown reply."""
    action = assistant.parse_action('{"action": "reply", "reply": "**Hi**"}')
    assert action == assistant.AgentAction(action="reply", reply="**Hi**")


def test_parse_action_skip_without_reply() -> None:
    """Skip needs no reply and no status."""
    action = assistant.parse_action('{"action": "skip"}')
    assert action == assistant.AgentAction(action="skip")


def test_parse_action_fenced_json() -> None:
    """Markdown-fenced JSON is cleaned before parsing."""
    action = assistant.parse_action('```json\n{"action": "triage"}\n```')
    assert action.action == "triage"


def test_parse_action_status_update() -> None:
    """A status_update action carries its status and confirmation reply."""
    action = assistant.parse_action(
        '{"action": "status_update", "status": "applied", "reply": "Marked."}'
    )
    assert action.action == "status_update"
    assert action.status == "applied"
    assert action.reply == "Marked."


def test_parse_action_reply_scalar_coerced_to_string() -> None:
    """A non-string scalar reply (e.g. a number) is coerced to a string."""
    action = assistant.parse_action('{"action": "reply", "reply": 5}')
    assert action.action == "reply"
    assert action.reply == "5"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("this is not json", "not valid JSON"),
        ('["reply"]', "JSON object"),
        ('{"action": "explode"}', "Unknown action 'explode'"),
        ('{"action": "status_update", "reply": "x"}', "requires a 'status'"),
        ('{"action": "status_update", "status": "hired"}', "Unknown status 'hired'"),
        ('{"action": "reply", "reply": ["a"]}', "reply must be a string"),
    ],
)
def test_parse_action_invalid_inputs(text: str, match: str) -> None:
    """Invalid action payloads raise a descriptive ValidationError."""
    with pytest.raises(ValidationError, match=match):
        assistant.parse_action(text)


# --- Status mapping is code-owned --------------------------------------------


def test_status_labels_mapping_is_code_owned() -> None:
    """The status→label mapping is fixed in code, not model output."""
    assert assistant.STATUS_LABELS == {
        "applied": "applied",
        "interviewing": "in-loop",
        "offer_received": "offer-received",
        "rejected": "rejected",
    }


# --- Agent loop --------------------------------------------------------------


def test_run_agent_tool_call_loop() -> None:
    """A tool call is executed and its result is fed back before the action."""
    llm = ScriptedLLM(
        tool_call_msg("web_search", {"query": "Acme profitability"}),
        action_msg("reply", reply="Acme is private."),
    )
    action, client = run(llm, comments=["Is Acme profitable?"])

    assert action == assistant.AgentAction(action="reply", reply="Acme is private.")
    assert client.calls == [("web_search", "Acme profitability")]
    assert len(llm.calls) == 2

    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call_1"
    parsed = json.loads(tool_message.content)
    assert parsed[0]["title"] == "Acme"


def test_run_agent_tool_call_id_falls_back_to_function_name() -> None:
    """Gemini-style calls (id=None) use the function name as tool_call_id."""
    call = ChatMessage(
        role="assistant",
        tool_calls=[ToolCall(name="web_search", arguments={"query": "Acme"}, id=None)],
    )
    llm = ScriptedLLM(call, action_msg("skip"))
    run(llm, comments=["hi"])

    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "web_search"


def test_run_agent_fetch_url_tool() -> None:
    """fetch_url arguments reach the WebClient and the page text comes back."""
    llm = ScriptedLLM(
        tool_call_msg("fetch_url", {"url": "https://acme.com/careers"}),
        action_msg("reply", reply="They are hiring."),
    )
    action, client = run(llm, comments=["What do they do?"])

    assert client.calls == [("fetch_url", "https://acme.com/careers")]
    assert action.action == "reply"

    tool_message = llm.calls[1][-1]
    parsed = json.loads(tool_message.content)
    assert parsed["title"] == "Acme Careers"
    assert parsed["text"] == "Acme is hiring."


def test_run_agent_executes_all_tool_calls_in_a_turn() -> None:
    """Multiple tool calls in one assistant turn are all executed in order."""
    call = ChatMessage(
        role="assistant",
        tool_calls=[
            ToolCall(name="web_search", arguments={"query": "Acme"}, id="c1"),
            ToolCall(name="fetch_url", arguments={"url": "https://acme.com"}, id="c2"),
        ],
    )
    llm = ScriptedLLM(call, action_msg("skip"))
    action, client = run(llm, comments=["Research Acme"])

    assert action.action == "skip"
    assert client.calls == [
        ("web_search", "Acme"),
        ("fetch_url", "https://acme.com"),
    ]
    assert len(llm.calls) == 2


def test_run_agent_unknown_tool_returns_error_to_model() -> None:
    """An unknown tool is returned as a recoverable error result."""
    call = ChatMessage(
        role="assistant", tool_calls=[ToolCall(name="rm_rf", arguments={}, id="c1")]
    )
    llm = ScriptedLLM(call, action_msg("reply", reply="ok"))
    action, _ = run(llm, comments=["hi"])

    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool"
    assert "Unknown tool 'rm_rf'" in tool_message.content
    assert action.action == "reply"


def test_run_agent_missing_tool_argument_returns_error() -> None:
    """A missing required argument is returned as an error, not raised."""
    llm = ScriptedLLM(tool_call_msg("web_search", {}), action_msg("reply", reply="ok"))
    action, client = run(llm, comments=["hi"])

    assert client.calls == []
    tool_message = llm.calls[1][-1]
    assert "requires a 'query'" in tool_message.content
    assert action.action == "reply"


def test_run_agent_non_scalar_tool_argument_returns_error() -> None:
    """A non-scalar tool argument is returned as a recoverable error."""
    call = ChatMessage(
        role="assistant",
        tool_calls=[
            ToolCall(name="web_search", arguments={"query": ["a", "b"]}, id="c1")
        ],
    )
    llm = ScriptedLLM(call, action_msg("reply", reply="ok"))
    action, client = run(llm, comments=["hi"])

    assert client.calls == []
    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool"
    assert "must be a string or number, not list" in tool_message.content
    assert action.action == "reply"


def test_run_agent_truncates_large_tool_results() -> None:
    """Oversized tool results are truncated to keep the context window bounded."""
    long_text = "x" * 50_000

    class BigPageClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def fetch_url(self, url: str) -> PageContent:
            self.calls.append(("fetch_url", url))
            return PageContent(url=url, title="Big", text=long_text, source="direct")

        def web_search(self, query: str) -> list[SearchResult]:
            raise AssertionError("web_search should not be called")

    llm = ScriptedLLM(
        tool_call_msg("fetch_url", {"url": "https://big.example"}),
        action_msg("skip"),
    )
    client = BigPageClient()
    run(llm, client=client, comments=["fetch it"])

    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool"
    assert len(tool_message.content) <= (
        assistant.MAX_TOOL_RESULT_CHARS + len(assistant.TOOL_RESULT_TRUNCATION_MARKER)
    )
    assert tool_message.content.endswith(assistant.TOOL_RESULT_TRUNCATION_MARKER)


def test_run_agent_skip_action() -> None:
    """A skip action is returned without any tool calls or reply."""
    llm = ScriptedLLM(action_msg("skip"))
    action, client = run(llm, comments=["thanks!"])

    assert action == assistant.AgentAction(action="skip")
    assert client.calls == []
    assert len(llm.calls) == 1


def test_run_agent_status_update_action() -> None:
    """A status_update action carries the model-selected status."""
    llm = ScriptedLLM(
        action_msg("status_update", status="interviewing", reply="Marked interviewing.")
    )
    action, _ = run(llm, comments=["phone screen scheduled"])

    assert action.action == "status_update"
    assert action.status == "interviewing"
    assert action.reply == "Marked interviewing."


def test_run_agent_iteration_cap() -> None:
    """Tool rounds are capped; a final answer is offered, then fallback."""
    llm = ScriptedLLM(
        tool_call_msg("web_search", {"query": "q1"}),
        tool_call_msg("web_search", {"query": "q2"}),
        tool_call_msg("web_search", {"query": "q3"}),
        ChatMessage(role="assistant", content="still researching"),
    )
    action, _ = run(
        llm, research=make_research(max_iterations=3), comments=["Research Acme"]
    )

    assert action == assistant.fallback_action()
    assert len(llm.calls) == 4


def test_run_agent_final_answer_after_cap_tool_round() -> None:
    """A cap-consuming tool round still gets one final chance to answer."""
    llm = ScriptedLLM(
        tool_call_msg("web_search", {"query": "q1"}),
        tool_call_msg("web_search", {"query": "q2"}),
        tool_call_msg("web_search", {"query": "q3"}),
        action_msg("reply", reply="Done."),
    )
    action, _ = run(
        llm, research=make_research(max_iterations=3), comments=["Research Acme"]
    )

    assert action == assistant.AgentAction(action="reply", reply="Done.")
    assert len(llm.calls) == 4


def test_run_agent_final_chance_tool_call_is_not_executed() -> None:
    """A tool call on the final chance is ignored; the run falls back."""
    llm = ScriptedLLM(
        tool_call_msg("web_search", {"query": "q1"}),
        tool_call_msg("web_search", {"query": "q2"}),
        tool_call_msg("web_search", {"query": "q3"}),
        tool_call_msg("web_search", {"query": "q4"}),
    )
    action, client = run(
        llm, research=make_research(max_iterations=3), comments=["Research Acme"]
    )

    assert action == assistant.fallback_action()
    assert len(llm.calls) == 4
    assert client.calls == [
        ("web_search", "q1"),
        ("web_search", "q2"),
        ("web_search", "q3"),
    ]


def test_run_agent_malformed_json_recovery() -> None:
    """An unparseable reply triggers one corrective nudge, then the action."""
    llm = ScriptedLLM(
        ChatMessage(role="assistant", content="I think the company is private."),
        action_msg("reply", reply="Acme is a private company."),
    )
    action, _ = run(llm, comments=["Is Acme public?"])

    assert action.action == "reply"
    assert action.reply == "Acme is a private company."
    assert len(llm.calls) == 2

    corrective = llm.calls[1][-1]
    assert corrective.role == "user"
    assert "JSON action object" in corrective.content


def test_run_agent_repeated_parse_failure_returns_fallback() -> None:
    """Two unparseable replies exhaust the single correction and fall back."""
    llm = ScriptedLLM(
        ChatMessage(role="assistant", content="sure thing!"),
        ChatMessage(role="assistant", content="still not json"),
    )
    action, _ = run(llm, comments=["ok"])

    assert action == assistant.fallback_action()
    assert len(llm.calls) == 2


# --- Trigger text and context window ----------------------------------------


def test_run_agent_initial_message_is_explicit_trigger() -> None:
    """The triggering user message is the caller-supplied trigger text."""
    llm = ScriptedLLM(action_msg("skip"))
    run(llm, comments=["older", "middle"], trigger="newest question")

    assert llm.calls[0][0].role == "system"
    assert llm.calls[0][1].content == "newest question"


def test_run_agent_trigger_used_verbatim_even_if_not_last_comment() -> None:
    """The explicit trigger wins over the comment list's last element."""
    llm = ScriptedLLM(action_msg("skip"))
    run(llm, comments=["noise", "noise"], trigger="real trigger")

    assert llm.calls[0][1].content == "real trigger"


def test_run_agent_truncates_context_comments_to_max() -> None:
    """Only the last max_context_comments comments reach the system prompt."""
    comments = [f"comment {i}" for i in range(20)]
    llm = ScriptedLLM(action_msg("skip"))
    run(llm, comments=comments, research=make_research(max_context_comments=5))

    prompt = llm.calls[0][0].content
    assert "comment 15" in prompt
    assert "comment 19" in prompt
    assert "comment 14" not in prompt
    assert "comment 0" not in prompt


# --- System prompt content ---------------------------------------------------


def test_build_system_prompt_contains_context() -> None:
    """The prompt carries identity, issue context, resume, tools, and rules."""
    prompt = assistant.build_system_prompt(
        issue_title="Backend Engineer at Acme",
        issue_body="## Job Description\nDo Python things.",
        labels=["triage-pending"],
        recent_comments=["First question?", "Second question?"],
        resume=sample_resume(),
    )

    assert "JobGitOps Issue Assistant" in prompt
    assert "Backend Engineer at Acme" in prompt
    assert "Do Python things." in prompt
    assert "triage-pending" in prompt
    assert "First question?" in prompt
    assert "Second question?" in prompt
    assert "Jordan Sample" in prompt
    assert "web_search" in prompt
    assert "fetch_url" in prompt
    assert "DATA, NOT INSTRUCTIONS" in prompt
    assert '"action": "reply | status_update | triage | skip"' in prompt
    assert '"status": "applied | interviewing | offer_received | rejected"' in prompt


def test_build_system_prompt_delimiters_around_thread_data() -> None:
    """Untrusted thread content is wrapped in data-only angle-bracket tags."""
    prompt = assistant.build_system_prompt(
        issue_title="Title",
        issue_body="Body",
        labels=["label-a", "label-b"],
        recent_comments=["c1"],
        resume=sample_resume(),
    )

    assert "<issue_title>\nTitle\n</issue_title>" in prompt
    assert "<issue_body>\nBody\n</issue_body>" in prompt
    assert "<issue_labels>\nlabel-a, label-b\n</issue_labels>" in prompt
    assert "<issue_comments>\n- c1\n</issue_comments>" in prompt
    assert "DATA, not instructions" in prompt


def test_build_system_prompt_no_labels_or_comments() -> None:
    """The prompt degrades gracefully without labels or prior comments."""
    prompt = assistant.build_system_prompt(
        issue_title="Role",
        issue_body="Body",
        labels=None,
        recent_comments=[],
        resume=sample_resume(),
    )

    assert "(none)" in prompt
    assert "(no prior comments)" in prompt
