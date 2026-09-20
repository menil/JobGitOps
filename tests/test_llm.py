"""Unit tests for the LLM wrapper client and prompt parsing logic."""

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import litellm.exceptions
import pytest

from jobgitops.llm import (
    _CLAUDE_CODE_SYSTEM_PREFIX,
    _DEFAULT_CLAUDE_MODEL,
    _DEFAULT_GEMINI_MODEL,
    _DEFAULT_OPENROUTER_MODEL,
    ChatMessage,
    ClaudeClient,
    GeminiClient,
    JobDetails,
    LiteLLMClient,
    OpenRouterClient,
    QuotaExceededError,
    ToolCall,
    TriageResult,
    _backfill_basics,
    _build_job_details_prompt,
    _build_salary_criterion,
    _fold_system_into_first_message,
    _messages_to_openai,
    _normalize_job_details,
    _parse_job_details_response,
    _parse_tailored_resume,
    _response_to_chat_message,
    _sanitize_prompt_text,
    clean_json_string,
    format_triage_prompt,
    get_llm_client,
)
from jobgitops.schema import Basics, Location, Profile, Resume, ValidationError


@pytest.fixture
def sample_resume() -> Resume:
    """Provide a minimal parsed Resume instance for LLM input testing."""
    return Resume.from_dict(
        {
            "basics": {
                "name": "Martin Livne",
                "label": "Principal Software Engineer",
                "email": "martin@example.com",
                "summary": "Experienced python developer.",
                "location": {"city": "Seattle", "region": "WA", "countryCode": "US"},
            },
            "work": [
                {
                    "name": "Tech Corp",
                    "position": "Senior Backend Engineer",
                    "startDate": "2020-01-01",
                    "endDate": "2024-01-01",
                    "highlights": [
                        "Designed microservices in Python",
                        "Built APIs",
                    ],
                }
            ],
            "skills": [{"name": "Languages", "keywords": ["Python", "Go", "SQL"]}],
        }
    )


def _mock_completion_response(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a fake litellm ModelResponse."""
    msg = MagicMock()
    msg.content = content
    if tool_calls:
        calls = []
        for tc in tool_calls:
            c = MagicMock()
            c.id = tc.get("id", "call_1")
            c.type = "function"
            func = MagicMock()
            func.name = tc.get("name", "")
            func.arguments = (
                json.dumps(tc.get("arguments", {}))
                if isinstance(tc.get("arguments"), dict)
                else str(tc.get("arguments", "{}"))
            )
            c.function = func
            calls.append(c)
        msg.tool_calls = calls
    else:
        msg.tool_calls = None

    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


# --- TriageResult and JobDetails tests ---------------------------------------


def test_triage_result_from_dict_success() -> None:
    """Verify successful parsing of valid triage dictionary data."""
    data = {
        "fit_score": 4.5,
        "tech_stack_fit": 4.0,
        "experience_fit": 5.0,
        "location_fit": 5.0,
        "salary_fit": 4.0,
        "industry_fit": 4.5,
        "reasoning": "Strong match with Python and backend design.",
    }
    result = TriageResult.from_dict(data)
    assert result.fit_score == 4.5
    assert result.tech_stack_fit == 4.0
    assert result.experience_fit == 5.0
    assert result.location_fit == 5.0
    assert result.salary_fit == 4.0
    assert result.industry_fit == 4.5
    assert result.reasoning == "Strong match with Python and backend design."


def test_triage_result_from_dict_structured_reasoning() -> None:
    """Verify TriageResult preserves multi-line structured reasoning strings."""
    structured_reasoning = (
        "Strong overall alignment with principal requirements.\n\n"
        "- **Tech Stack Match:** Direct experience with Python, FastAPI, "
        "and Kubernetes.\n"
        "- **Experience & Years Fit:** 10+ years backend aligns with Staff/Principal "
        "scope.\n"
        "- **Location & Timezone Suitability:** Hybrid in Seattle is local to "
        "your residence.\n"
        "- **Salary Alignment:** Stated comp range is well above target minimum.\n"
        "- **Industry Domain Familiarity:** Deep background in developer tools.\n\n"
        "Overall, an outstanding technical and cultural match."
    )
    data = {
        "fit_score": 4.8,
        "tech_stack_fit": 5.0,
        "experience_fit": 5.0,
        "location_fit": 5.0,
        "salary_fit": 4.5,
        "industry_fit": 4.5,
        "reasoning": structured_reasoning,
    }
    result = TriageResult.from_dict(data)
    assert result.reasoning == structured_reasoning


def test_triage_prompt_requests_bulleted_reasoning() -> None:
    """Verify TRIAGE_PROMPT contains instructions for paragraphs and bullet points."""
    from jobgitops.llm import TRIAGE_PROMPT

    assert "markdown paragraphs and bullet points" in TRIAGE_PROMPT
    assert "- **Tech Stack Match:**" in TRIAGE_PROMPT
    assert "- **Experience & Years Fit:**" in TRIAGE_PROMPT
    assert "- **Location & Timezone Suitability:**" in TRIAGE_PROMPT
    assert "- **Salary Alignment:**" in TRIAGE_PROMPT
    assert "- **Industry Domain Familiarity:**" in TRIAGE_PROMPT
    assert "CRITICAL: Use actual newlines" in TRIAGE_PROMPT


def test_triage_result_from_dict_missing_reasoning_default() -> None:
    """Verify that a missing or null reasoning field defaults to empty string."""
    data = {
        "fit_score": 4.0,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
    }
    result = TriageResult.from_dict(data)
    assert result.reasoning == ""

    data_null = data.copy()
    data_null["reasoning"] = None
    result_null = TriageResult.from_dict(data_null)
    assert result_null.reasoning == ""


def test_triage_result_from_dict_invalid_reasoning_types() -> None:
    """Verify reasoning field validation rejects booleans and collections."""
    base_data = {
        "fit_score": 4.0,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
    }

    bad_bool = base_data.copy()
    bad_bool["reasoning"] = True
    with pytest.raises(ValidationError, match="reasoning must be a string"):
        TriageResult.from_dict(bad_bool)

    bad_list = base_data.copy()
    bad_list["reasoning"] = ["great match", "some gaps"]
    with pytest.raises(ValidationError, match="reasoning must be a string"):
        TriageResult.from_dict(bad_list)


def test_triage_result_from_dict_invalid_types_and_bounds() -> None:
    """Verify parser enforces data bounds, correct types, and dictionary input."""
    with pytest.raises(ValidationError, match="Triage result must be a dictionary"):
        TriageResult.from_dict("not-a-dict")  # type: ignore

    missing_fields = {
        "fit_score": 4.0,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Reasoning",
    }
    with pytest.raises(ValidationError, match="Missing required field"):
        TriageResult.from_dict(missing_fields)

    bad_type = {
        "fit_score": "excellent",
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Reasoning",
    }
    with pytest.raises(ValidationError, match="must be a number"):
        TriageResult.from_dict(bad_type)

    bad_bool_score = {
        "fit_score": True,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Reasoning",
    }
    with pytest.raises(ValidationError, match="must be a number, not a boolean"):
        TriageResult.from_dict(bad_bool_score)

    out_of_bounds_high = {
        "fit_score": 5.5,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Reasoning",
    }
    with pytest.raises(ValidationError, match="must be between 1.0 and 5.0"):
        TriageResult.from_dict(out_of_bounds_high)

    out_of_bounds_low = {
        "fit_score": 0.5,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Reasoning",
    }
    with pytest.raises(ValidationError, match="must be between 1.0 and 5.0"):
        TriageResult.from_dict(out_of_bounds_low)


def test_job_details_from_dict() -> None:
    """Verify JobDetails model validation and serialization."""
    data = {
        "company": "Acme Corp",
        "role": "Backend Engineer",
        "location": "Seattle, WA",
        "salary": "$150,000",
    }
    details = JobDetails.from_dict(data)
    assert details.company == "Acme Corp"
    assert details.role == "Backend Engineer"
    assert details.location == "Seattle, WA"
    assert details.salary == "$150,000"
    assert details.to_dict() == data

    # Missing fields default to empty string
    partial = JobDetails.from_dict({"company": "Acme"})
    assert partial.company == "Acme"
    assert partial.role == ""
    assert partial.location == ""
    assert partial.salary == ""

    # Invalid input types raise ValidationError
    with pytest.raises(ValidationError, match="must return a JSON object"):
        JobDetails.from_dict("invalid")

    with pytest.raises(ValidationError, match="must be a string, not a boolean"):
        JobDetails.from_dict({"company": True})

    with pytest.raises(ValidationError, match="must be a string, not a collection"):
        JobDetails.from_dict({"company": ["Acme"]})


def test_clean_json_string() -> None:
    """Verify cleaning utility successfully extracts JSON from various formats."""
    assert clean_json_string('{"a": 1}') == '{"a": 1}'

    fenced_label = """```json
{"a": 1}
```"""
    assert clean_json_string(fenced_label) == '{"a": 1}'

    pre_post = """Here is the output:
```json
{"a": 1}
```
Hope that helps!"""
    assert clean_json_string(pre_post) == '{"a": 1}'


# --- get_llm_client provider resolution tests --------------------------------


@patch.dict(os.environ, {}, clear=True)
def test_get_llm_client_missing_config() -> None:
    """Verify get_llm_client raises ValidationError when no credentials exist."""
    with pytest.raises(ValidationError, match="No LLM provider configured"):
        get_llm_client()


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=True)
def test_get_llm_client_default_gemini() -> None:
    """Verify Gemini is selected by default when only GEMINI_API_KEY is present."""
    client = get_llm_client()
    assert isinstance(client, GeminiClient)
    assert client.api_key == "fake-gemini-key"
    assert client.model_name == _DEFAULT_GEMINI_MODEL
    assert client.litellm_model == "gemini/models/gemini-2.5-flash"


@patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-or-key"}, clear=True)
def test_get_llm_client_default_openrouter() -> None:
    """Verify OpenRouter is selected by default when key is present."""
    client = get_llm_client()
    assert isinstance(client, OpenRouterClient)
    assert client.api_key == "fake-or-key"
    assert client.model_name == _DEFAULT_OPENROUTER_MODEL
    assert client.litellm_model == "openrouter/openrouter/free"


@patch.dict(
    os.environ,
    {
        "GEMINI_API_KEY": "fake-gemini-key",
        "OPENROUTER_API_KEY": "fake-or-key",
        "LLM_PROVIDER": "OpenRouter",
        "OPENROUTER_MODEL": "meta-llama/llama-3-70b-instruct",
    },
    clear=True,
)
def test_get_llm_client_explicit_provider() -> None:
    """Verify LLM_PROVIDER env variable overrides default behavior."""
    client = get_llm_client()
    assert isinstance(client, OpenRouterClient)
    assert client.api_key == "fake-or-key"
    assert client.model_name == "meta-llama/llama-3-70b-instruct"
    assert client.litellm_model == "openrouter/meta-llama/llama-3-70b-instruct"


@patch.dict(
    os.environ,
    {
        "GEMINI_API_KEY": "custom-key",
        "LLM_PROVIDER": "Gemini",
        "GEMINI_MODEL": "gemini-pro-custom",
    },
    clear=True,
)
def test_get_llm_client_custom_gemini_model() -> None:
    """Verify custom Gemini model can be configured through environment variables."""
    client = get_llm_client()
    assert isinstance(client, GeminiClient)
    assert client.model_name == "gemini-pro-custom"
    assert client.litellm_model == "gemini/gemini-pro-custom"


@patch.dict(
    os.environ,
    {
        "GEMINI_API_KEY": "fake-gemini-key",
        "GEMINI_MODEL": "gemini-pro-env",
    },
    clear=True,
)
def test_get_llm_client_gemini_model_override() -> None:
    """Verify the research.model override wins over GEMINI_MODEL for the responder."""
    client = get_llm_client(model="models/gemini-2.5-flash")
    assert isinstance(client, GeminiClient)
    assert client.model_name == "models/gemini-2.5-flash"
    assert client.litellm_model == "gemini/models/gemini-2.5-flash"


@patch.dict(
    os.environ,
    {
        "OPENROUTER_API_KEY": "fake-or-key",
        "OPENROUTER_MODEL": "meta-llama/llama-3-70b-instruct",
    },
    clear=True,
)
def test_get_llm_client_openrouter_model_override() -> None:
    """Verify the research.model override wins over OPENROUTER_MODEL."""
    client = get_llm_client(model="google/gemini-2.5-flash")
    assert isinstance(client, OpenRouterClient)
    assert client.model_name == "google/gemini-2.5-flash"
    assert client.litellm_model == "openrouter/google/gemini-2.5-flash"


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=True)
def test_get_llm_client_invalid_override_raises() -> None:
    """Verify an invalid model override fails Gemini validation with the name."""
    with pytest.raises(ValidationError, match="Invalid model name: 'gpt-4o'"):
        get_llm_client(model="gpt-4o")


@patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-or-key"}, clear=True)
def test_get_llm_client_invalid_openrouter_override_raises() -> None:
    """Verify an invalid model override fails OpenRouter validation with the name."""
    with pytest.raises(ValidationError, match="Invalid model name: 'gpt-4o'"):
        get_llm_client(model="gpt-4o")


@patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}, clear=True)
def test_get_llm_client_explicit_gemini_missing_key() -> None:
    """Verify missing key raises ValidationError when provider is explicitly set."""
    msg = "GEMINI_API_KEY environment variable is missing"
    with pytest.raises(ValidationError, match=msg):
        get_llm_client()


@patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=True)
def test_get_llm_client_explicit_openrouter_missing_key() -> None:
    """Verify missing key raises ValidationError when provider is explicitly set."""
    msg = "OPENROUTER_API_KEY environment variable is missing"
    with pytest.raises(ValidationError, match=msg):
        get_llm_client()


@patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"}, clear=True)
def test_get_llm_client_default_claude_token() -> None:
    """Verify Claude is selected by default when CLAUDE_CODE_OAUTH_TOKEN is present."""
    client = get_llm_client()
    assert isinstance(client, ClaudeClient)
    assert client.api_key == "sk-ant-oat01-test"
    assert client.model_name == _DEFAULT_CLAUDE_MODEL
    assert client.litellm_model == "anthropic/claude-sonnet-5"


@patch.dict(os.environ, {"CLAUDE_API_KEY": "sk-ant-api03-test"}, clear=True)
def test_get_llm_client_claude_api_key() -> None:
    """Verify Claude is selected when CLAUDE_API_KEY is present."""
    client = get_llm_client()
    assert isinstance(client, ClaudeClient)
    assert client.api_key == "sk-ant-api03-test"
    assert client.model_name == _DEFAULT_CLAUDE_MODEL


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-test2"}, clear=True)
def test_get_llm_client_anthropic_api_key() -> None:
    """Verify Claude is selected when ANTHROPIC_API_KEY is present."""
    client = get_llm_client()
    assert isinstance(client, ClaudeClient)
    assert client.api_key == "sk-ant-api03-test2"
    assert client.model_name == _DEFAULT_CLAUDE_MODEL


@patch.dict(
    os.environ,
    {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test",
        "LLM_PROVIDER": "Claude",
        "CLAUDE_MODEL": "claude-3-opus-20240229",
    },
    clear=True,
)
def test_get_llm_client_custom_claude_model() -> None:
    """Verify custom Claude model can be configured through environment variables."""
    client = get_llm_client()
    assert isinstance(client, ClaudeClient)
    assert client.model_name == "claude-3-opus-20240229"
    assert client.litellm_model == "anthropic/claude-3-opus-20240229"


@patch.dict(
    os.environ,
    {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"},
    clear=True,
)
def test_get_llm_client_claude_model_override() -> None:
    """Verify the research.model override wins over CLAUDE_MODEL."""
    client = get_llm_client(model="claude-3-5-haiku-20241022")
    assert isinstance(client, ClaudeClient)
    assert client.model_name == "claude-3-5-haiku-20241022"


@patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"}, clear=True)
def test_get_llm_client_invalid_claude_override_raises() -> None:
    """Verify an invalid model override fails Claude validation with the name."""
    with pytest.raises(ValidationError, match="Invalid model name: 'gpt-4o'"):
        get_llm_client(model="gpt-4o")


@patch.dict(
    os.environ,
    {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-primary",
        "CLAUDE_API_KEY": "sk-ant-api03-secondary",
        "ANTHROPIC_API_KEY": "sk-ant-api03-tertiary",
    },
    clear=True,
)
def test_get_llm_client_claude_key_priority() -> None:
    """Verify CLAUDE_CODE_OAUTH_TOKEN takes priority over other Claude keys."""
    client = get_llm_client()
    assert isinstance(client, ClaudeClient)
    assert client.api_key == "sk-ant-oat01-primary"


@patch.dict(os.environ, {"LLM_PROVIDER": "claude"}, clear=True)
def test_get_llm_client_explicit_claude_missing_key() -> None:
    """Verify missing key raises ValidationError when provider is explicitly set."""
    with pytest.raises(ValidationError, match=r"(?i)claude.*missing"):
        get_llm_client()


@patch.dict(
    os.environ,
    {"LLM_PROVIDER": "invalid-provider", "GEMINI_API_KEY": "key"},
    clear=True,
)
def test_get_llm_client_invalid_provider() -> None:
    """Verify invalid provider string raises ValidationError."""
    with pytest.raises(ValidationError, match="Unknown LLM provider specified"):
        get_llm_client()


# --- GeminiClient tests ------------------------------------------------------


@patch("litellm.completion")
def test_gemini_client_triage(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient generates triage results successfully."""
    triage_payload = {
        "fit_score": 4.0,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Standard match",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(triage_payload))

    client = GeminiClient(api_key="key")
    res = client.triage_job("Python role in Seattle", sample_resume)

    assert res.fit_score == 4.0
    assert res.reasoning == "Standard match"
    mock_completion.assert_called_once()
    called_kwargs = mock_completion.call_args[1]
    assert called_kwargs["model"] == "gemini/models/gemini-2.5-flash"
    assert called_kwargs["api_key"] == "key"


@patch("litellm.completion")
def test_gemini_client_tailor(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient returns a valid, updated Resume object after tailoring."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Highly tailored Python profile."
    mock_completion.return_value = _mock_completion_response(json.dumps(tailored_data))

    client = GeminiClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.summary == "Highly tailored Python profile."
    assert tailored_resume.basics.name == "Martin Livne"


def test_backfill_basics_restores_all_dropped_optional_fields() -> None:
    """Every optional basics field falls back to the original when tailored omits it."""
    original = Basics(
        name="Original Name",
        label="Original Label",
        email="orig@example.com",
        phone="555-0100",
        url="https://orig.example",
        summary="Original summary",
        location=Location(city="Seattle", state="WA", country_code="US"),
        profiles=[Profile(network="github", username="orig")],
    )
    tailored = Basics(name="Original Name")

    result = _backfill_basics(tailored, original)

    assert result.label == "Original Label"
    assert result.email == "orig@example.com"
    assert result.phone == "555-0100"
    assert result.url == "https://orig.example"
    assert result.summary == "Original summary"
    assert result.location == Location(city="Seattle", state="WA", country_code="US")
    assert result.profiles == [Profile(network="github", username="orig")]


def test_backfill_basics_preserves_tailored_values_when_present() -> None:
    """A field the model did set is kept, not clobbered by the original's value."""
    original = Basics(
        name="Original Name",
        label="Original Label",
        email="orig@example.com",
        phone="555-0100",
        url="https://orig.example",
        summary="Original summary",
        location=Location(city="Seattle", state="WA", country_code="US"),
        profiles=[Profile(network="github", username="orig")],
    )
    tailored = Basics(
        name="Original Name",
        label="New Label",
        email="new@example.com",
        phone="555-0200",
        url="https://new.example",
        summary="New summary",
        location=Location(city="Austin", state="TX", country_code="US"),
        profiles=[Profile(network="linkedin", username="new")],
    )

    result = _backfill_basics(tailored, original)

    assert result.label == "New Label"
    assert result.email == "new@example.com"
    assert result.phone == "555-0200"
    assert result.url == "https://new.example"
    assert result.summary == "New summary"
    assert result.location == Location(city="Austin", state="TX", country_code="US")
    assert result.profiles == [Profile(network="linkedin", username="new")]


@patch("litellm.completion")
def test_gemini_client_tailor_backfills_dropped_label(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """A tailored response that omits basics.label keeps the original label."""
    tailored_data = sample_resume.to_dict()
    del tailored_data["basics"]["label"]
    mock_completion.return_value = _mock_completion_response(json.dumps(tailored_data))

    client = GeminiClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.label == "Principal Software Engineer"


@patch("litellm.completion")
def test_gemini_client_tailor_retries_once_on_malformed_json(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify a malformed tailoring response is retried once and then succeeds."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored after retry."
    mock_completion.side_effect = [
        _mock_completion_response("not json at all"),
        _mock_completion_response(json.dumps(tailored_data)),
    ]

    client = GeminiClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.summary == "Tailored after retry."
    assert mock_completion.call_count == 2


@patch("litellm.completion")
def test_gemini_client_tailor_fails_after_single_retry(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify persistent malformed output raises ValidationError after one retry."""
    mock_completion.return_value = _mock_completion_response("{broken")

    client = GeminiClient(api_key="key")

    with pytest.raises(ValidationError, match="Gemini resume tailoring failed"):
        client.tailor_resume("Python role", sample_resume)

    assert mock_completion.call_count == 2


@patch("litellm.completion")
def test_gemini_client_tailor_retries_on_schema_invalid_json(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify schema-invalid (but parseable) output is retried once."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored after schema retry."
    mock_completion.side_effect = [
        _mock_completion_response('{"status": "ok"}'),
        _mock_completion_response(json.dumps(tailored_data)),
    ]

    client = GeminiClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.summary == "Tailored after schema retry."
    assert mock_completion.call_count == 2


@patch("litellm.completion")
def test_gemini_client_quota_error_does_not_retry(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify quota errors abort immediately without a second API call."""
    mock_completion.side_effect = litellm.exceptions.RateLimitError(
        message="Rate limit exceeded", model="gemini-2.5-flash", llm_provider="gemini"
    )

    client = GeminiClient(api_key="key")

    with pytest.raises(QuotaExceededError, match="LLM API quota exceeded"):
        client.tailor_resume("Python role", sample_resume)

    assert mock_completion.call_count == 1


@patch("litellm.completion")
def test_gemini_client_failure_handling(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient exceptions are caught and raised as ValidationError."""
    mock_completion.side_effect = litellm.exceptions.APIError(
        status_code=500,
        message="API error",
        llm_provider="gemini",
        model="gemini-2.5-flash",
    )

    client = GeminiClient(api_key="key")

    with pytest.raises(ValidationError, match="LLM request failed"):
        client.triage_job("Python role", sample_resume)

    with pytest.raises(ValidationError, match="LLM request failed"):
        client.tailor_resume("Python role", sample_resume)


# --- OpenRouterClient tests --------------------------------------------------


@patch("litellm.completion")
def test_openrouter_client_triage(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouterClient performs triage successfully."""
    triage_payload = {
        "fit_score": 4.2,
        "tech_stack_fit": 4.5,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.5,
        "reasoning": "Matches stack preferences.",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(triage_payload))

    client = OpenRouterClient(api_key="key")
    res = client.triage_job("Python engineer role", sample_resume)

    assert res.fit_score == 4.2
    assert res.reasoning == "Matches stack preferences."
    mock_completion.assert_called_once()
    called_kwargs = mock_completion.call_args[1]
    assert called_kwargs["model"] == "openrouter/openrouter/free"
    assert called_kwargs["api_key"] == "key"


@patch("litellm.completion")
def test_openrouter_client_tailor(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouterClient tailors resume successfully."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored by OpenRouter."
    mock_completion.return_value = _mock_completion_response(json.dumps(tailored_data))

    client = OpenRouterClient(api_key="key")
    tailored = client.tailor_resume("Python job", sample_resume)

    assert tailored.basics.summary == "Tailored by OpenRouter."


@patch("litellm.completion")
def test_openrouter_client_tailor_backfills_dropped_label(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify dropped label in OpenRouter tailored resume is backfilled."""
    tailored_data = sample_resume.to_dict()
    del tailored_data["basics"]["label"]
    mock_completion.return_value = _mock_completion_response(json.dumps(tailored_data))

    client = OpenRouterClient(api_key="key")
    tailored = client.tailor_resume("Python job", sample_resume)

    assert tailored.basics.label == "Principal Software Engineer"


@patch("litellm.completion")
def test_openrouter_client_failure_handling(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter error handling raises ValidationError."""
    mock_completion.side_effect = litellm.exceptions.APIError(
        status_code=500,
        message="OpenRouter server error",
        llm_provider="openrouter",
        model="openrouter/free",
    )

    client = OpenRouterClient(api_key="key")
    with pytest.raises(ValidationError, match="LLM request failed"):
        client.triage_job("Job desc", sample_resume)


@patch("litellm.completion")
def test_openrouter_client_quota_exceeded_handling(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter 429 raises QuotaExceededError."""
    mock_completion.side_effect = litellm.exceptions.RateLimitError(
        message="Rate limit exceeded",
        model="openrouter/free",
        llm_provider="openrouter",
    )

    client = OpenRouterClient(api_key="key")
    with pytest.raises(QuotaExceededError, match="LLM API quota exceeded"):
        client.triage_job("Job desc", sample_resume)


@patch("litellm.completion")
def test_openrouter_client_tailor_retries_once_on_malformed_json(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter retries once on malformed JSON response."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored on attempt 2."
    mock_completion.side_effect = [
        _mock_completion_response("not json"),
        _mock_completion_response(json.dumps(tailored_data)),
    ]

    client = OpenRouterClient(api_key="key")
    tailored = client.tailor_resume("Job desc", sample_resume)

    assert tailored.basics.summary == "Tailored on attempt 2."
    assert mock_completion.call_count == 2


@patch("litellm.completion")
def test_openrouter_client_tailor_fails_after_single_retry(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter fails after single retry on malformed JSON."""
    mock_completion.return_value = _mock_completion_response("{bad json")

    client = OpenRouterClient(api_key="key")
    with pytest.raises(ValidationError, match="OpenRouter resume tailoring failed"):
        client.tailor_resume("Job desc", sample_resume)

    assert mock_completion.call_count == 2


# --- Job details extraction tests --------------------------------------------


@patch("litellm.completion")
def test_gemini_client_extract_job_details(mock_completion: MagicMock) -> None:
    """Verify GeminiClient parses structured job details from a fetched page."""
    details = {
        "company": "Acme Corp",
        "role": "Senior Engineer",
        "location": "Seattle, WA",
        "salary": "$180,000",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(details))

    client = GeminiClient(api_key="key")
    extracted = client.extract_job_details(
        "Full text", "Page Title", "https://acme.com"
    )

    assert extracted == details


@patch("litellm.completion")
def test_gemini_client_extract_job_details_failure(
    mock_completion: MagicMock,
) -> None:
    """Verify failure during job details extraction raises ValidationError."""
    mock_completion.side_effect = Exception("API failure")

    client = GeminiClient(api_key="key")
    with pytest.raises(ValidationError, match="LLM request failed"):
        client.extract_job_details("Text", "Title", "https://acme.com")


@patch("litellm.completion")
def test_gemini_client_extract_job_details_quota(mock_completion: MagicMock) -> None:
    """Verify quota error during job details extraction raises QuotaExceededError."""
    mock_completion.side_effect = litellm.exceptions.RateLimitError(
        message="Quota exceeded", model="gemini-2.5-flash", llm_provider="gemini"
    )

    client = GeminiClient(api_key="key")
    with pytest.raises(QuotaExceededError, match="LLM API quota exceeded"):
        client.extract_job_details("Text", "Title", "https://acme.com")


@patch("litellm.completion")
def test_openrouter_client_extract_job_details(mock_completion: MagicMock) -> None:
    """Verify OpenRouterClient extracts job details correctly."""
    details = {
        "company": "Beta Inc",
        "role": "Staff Engineer",
        "location": "Remote",
        "salary": "$200,000",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(details))

    client = OpenRouterClient(api_key="key")
    extracted = client.extract_job_details("Text", "Title", "https://beta.com")

    assert extracted == details


@patch("litellm.completion")
def test_openrouter_client_extract_job_details_malformed(
    mock_completion: MagicMock,
) -> None:
    """Verify malformed JSON from extraction raises ValidationError."""
    mock_completion.return_value = _mock_completion_response("{bad json")

    client = OpenRouterClient(api_key="key")
    with pytest.raises(ValidationError):
        client.extract_job_details("Text", "Title", "https://beta.com")


# --- Chat tests --------------------------------------------------------------


@patch("litellm.completion")
def test_gemini_chat_plain_text(mock_completion: MagicMock) -> None:
    """Verify Gemini chat returns plain assistant text without tools."""
    mock_completion.return_value = _mock_completion_response("Hello from Gemini")

    client = GeminiClient(api_key="key")
    msg = client.chat([ChatMessage(role="user", content="Hi")])

    assert msg.role == "assistant"
    assert msg.content == "Hello from Gemini"
    assert msg.tool_calls is None


@patch("litellm.completion")
def test_gemini_chat_empty_response_raises(mock_completion: MagicMock) -> None:
    """Verify an empty response raises ValidationError."""
    mock_completion.return_value = _mock_completion_response("")

    client = GeminiClient(api_key="key")
    with pytest.raises(ValidationError, match="empty response"):
        client.chat([ChatMessage(role="user", content="Hi")])


@patch("litellm.completion")
def test_gemini_chat_mixed_text_and_tool_call(mock_completion: MagicMock) -> None:
    """Verify chat handles response with tool calls."""
    mock_completion.return_value = _mock_completion_response(
        content="Searching now",
        tool_calls=[{"name": "web_search", "arguments": {"query": "JobGitOps"}}],
    )

    client = GeminiClient(api_key="key")
    msg = client.chat(
        [ChatMessage(role="user", content="Search JobGitOps")],
        tools=[{"type": "function", "function": {"name": "web_search"}}],
    )

    assert msg.role == "assistant"
    assert msg.content == "Searching now"
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "web_search"
    assert msg.tool_calls[0].arguments == {"query": "JobGitOps"}


@patch("litellm.completion")
def test_gemini_chat_tool_call_round_trip(mock_completion: MagicMock) -> None:
    """Verify multi-turn tool message round-trip."""
    mock_completion.return_value = _mock_completion_response("Found 3 results.")

    client = GeminiClient(api_key="key")
    messages = [
        ChatMessage(role="user", content="Find jobs"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(name="web_search", arguments={"query": "python jobs"}, id="c1")
            ],
        ),
        ChatMessage(
            role="tool",
            content='{"results": ["Job A", "Job B"]}',
            tool_call_id="c1",
        ),
    ]
    msg = client.chat(messages)

    assert msg.role == "assistant"
    assert msg.content == "Found 3 results."
    mock_completion.assert_called_once()
    openai_msgs = mock_completion.call_args[1]["messages"]
    assert len(openai_msgs) == 3
    assert openai_msgs[2]["role"] == "tool"
    assert openai_msgs[2]["tool_call_id"] == "c1"


@patch("litellm.completion")
def test_gemini_chat_quota_exceeded(mock_completion: MagicMock) -> None:
    """Verify RateLimitError raises QuotaExceededError."""
    mock_completion.side_effect = litellm.exceptions.RateLimitError(
        message="Rate limit exceeded", model="gemini-2.5-flash", llm_provider="gemini"
    )

    client = GeminiClient(api_key="key")
    with pytest.raises(QuotaExceededError, match="LLM API quota exceeded"):
        client.chat([ChatMessage(role="user", content="Hi")])


@patch("litellm.completion")
def test_gemini_chat_api_error(mock_completion: MagicMock) -> None:
    """Verify generic API error raises ValidationError."""
    mock_completion.side_effect = litellm.exceptions.APIError(
        status_code=500,
        message="Internal server error",
        llm_provider="gemini",
        model="gemini-2.5-flash",
    )

    client = GeminiClient(api_key="key")
    with pytest.raises(ValidationError, match="LLM request failed"):
        client.chat([ChatMessage(role="user", content="Hi")])


@patch("litellm.completion")
def test_openrouter_chat_plain_text(mock_completion: MagicMock) -> None:
    """Verify OpenRouter chat returns plain assistant text."""
    mock_completion.return_value = _mock_completion_response("Hello from OpenRouter")

    client = OpenRouterClient(api_key="key")
    msg = client.chat([ChatMessage(role="user", content="Hi")])

    assert msg.role == "assistant"
    assert msg.content == "Hello from OpenRouter"


@patch("litellm.completion")
def test_openrouter_chat_tool_call_round_trip(mock_completion: MagicMock) -> None:
    """Verify OpenRouter chat tool calling round-trip."""
    mock_completion.return_value = _mock_completion_response(
        content="",
        tool_calls=[
            {
                "id": "call_abc",
                "name": "fetch_url",
                "arguments": {"url": "https://example.com"},
            }
        ],
    )

    client = OpenRouterClient(api_key="key")
    msg = client.chat(
        [ChatMessage(role="user", content="Fetch site")],
        tools=[{"type": "function", "function": {"name": "fetch_url"}}],
    )

    assert msg.tool_calls is not None
    assert msg.tool_calls[0].name == "fetch_url"
    assert msg.tool_calls[0].arguments == {"url": "https://example.com"}
    assert msg.tool_calls[0].id == "call_abc"


# --- ClaudeClient tests ------------------------------------------------------


@patch("litellm.completion")
def test_claude_client_triage_oauth_token(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify ClaudeClient triage with OAuth token."""
    triage_payload = {
        "fit_score": 4.6,
        "tech_stack_fit": 5.0,
        "experience_fit": 4.5,
        "location_fit": 4.5,
        "salary_fit": 4.0,
        "industry_fit": 4.5,
        "reasoning": "Strong match",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(triage_payload))

    client = ClaudeClient(api_key="sk-ant-oat01-test")
    res = client.triage_job("Python role", sample_resume)

    assert res.fit_score == 4.6
    called_kwargs = mock_completion.call_args[1]
    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["Authorization"] == "Bearer sk-ant-oat01-test"
    assert "claude-code" in called_kwargs["extra_headers"]["anthropic-beta"]


@patch("litellm.completion")
def test_claude_client_triage_api_key(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify ClaudeClient triage with standard API key."""
    triage_payload = {
        "fit_score": 4.0,
        "tech_stack_fit": 4.0,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.0,
        "reasoning": "Standard match",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(triage_payload))

    client = ClaudeClient(api_key="sk-ant-api03-test")
    res = client.triage_job("Python role", sample_resume)

    assert res.fit_score == 4.0
    called_kwargs = mock_completion.call_args[1]
    assert "extra_headers" not in called_kwargs
    assert called_kwargs["api_key"] == "sk-ant-api03-test"


@patch("litellm.completion")
def test_claude_client_tailor(
    mock_completion: MagicMock, sample_resume: Resume
) -> None:
    """Verify ClaudeClient tailor resume."""
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored by Claude."
    mock_completion.return_value = _mock_completion_response(json.dumps(tailored_data))

    client = ClaudeClient(api_key="sk-ant-api03-test")
    tailored = client.tailor_resume("Python role", sample_resume)

    assert tailored.basics.summary == "Tailored by Claude."


@patch("litellm.completion")
def test_claude_client_extract_job_details(mock_completion: MagicMock) -> None:
    """Verify ClaudeClient extract job details."""
    details = {
        "company": "Anthropic",
        "role": "AI Engineer",
        "location": "San Francisco, CA",
        "salary": "$250,000",
    }
    mock_completion.return_value = _mock_completion_response(json.dumps(details))

    client = ClaudeClient(api_key="sk-ant-api03-test")
    extracted = client.extract_job_details("Text", "Title", "https://anthropic.com")

    assert extracted == details


@patch("litellm.completion")
def test_claude_chat_plain_text(mock_completion: MagicMock) -> None:
    """Verify Claude chat returns assistant message."""
    mock_completion.return_value = _mock_completion_response("Hello from Claude")

    client = ClaudeClient(api_key="sk-ant-api03-test")
    msg = client.chat([ChatMessage(role="user", content="Hi")])

    assert msg.role == "assistant"
    assert msg.content == "Hello from Claude"


@patch("litellm.completion")
def test_claude_chat_oauth_folds_custom_system_into_first_message(
    mock_completion: MagicMock,
) -> None:
    """Verify OAuth token folds custom system text ahead of first user turn."""
    mock_completion.return_value = _mock_completion_response("Understood.")

    client = ClaudeClient(api_key="sk-ant-oat01-test")
    messages = [
        ChatMessage(role="system", content="Custom instructions"),
        ChatMessage(role="user", content="User message"),
    ]
    client.chat(messages)

    called_kwargs = mock_completion.call_args[1]
    assert called_kwargs["system"] == _CLAUDE_CODE_SYSTEM_PREFIX
    called_msgs = called_kwargs["messages"]
    assert len(called_msgs) == 1
    assert "Custom instructions" in called_msgs[0]["content"]
    assert "User message" in called_msgs[0]["content"]


# --- Prompt formatting and helper tests --------------------------------------


def test_parse_tailored_resume_preserves_original_meta(sample_resume: Resume) -> None:
    """Verify _parse_tailored_resume preserves the original resume's meta section."""
    original = Resume.from_dict(
        {
            "basics": {"name": "Jane Doe", "summary": "Original summary"},
            "meta": {
                "version": "v1.0.0",
                "themeOptions": {"fitPages": "auto"},
            },
        }
    )

    tailored_json = json.dumps(
        {
            "basics": {"name": "Jane Doe", "summary": "Tailored summary"},
            "work": [],
            "education": [],
            "skills": [],
            "projects": [],
        }
    )

    tailored_result = _parse_tailored_resume(
        fetch_text=lambda hint: tailored_json,
        provider_label="TestProvider",
        original=original,
    )

    assert tailored_result.meta == {
        "version": "v1.0.0",
        "themeOptions": {"fitPages": "auto"},
    }


def test_build_salary_criterion() -> None:
    """Verify salary criterion text generation."""
    crit_none = _build_salary_criterion(None)
    assert "if unspecified, grade 5.0" in crit_none

    crit_salary = _build_salary_criterion(180000)
    assert "$180,000/year" in crit_salary
    assert "scale the grade down proportionally" in crit_salary


def test_sanitize_prompt_text() -> None:
    """Verify prompt text sanitization escapes fences and strips control chars."""
    dirty = "Hello ```python dangerous()``` \x00world\r\n"
    clean = _sanitize_prompt_text(dirty)
    assert "```" not in clean
    assert "\x00" not in clean
    assert "world" in clean


def test_messages_to_openai_conversion() -> None:
    """Verify ChatMessage list is accurately converted to OpenAI format."""
    msgs = [
        ChatMessage(role="system", content="System text"),
        ChatMessage(role="user", content="User prompt"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(name="calc", arguments={"x": 1}, id="c_1")],
        ),
        ChatMessage(role="tool", content="Result 2", tool_call_id="c_1"),
    ]
    converted = _messages_to_openai(msgs)
    assert len(converted) == 4
    assert converted[0] == {"role": "system", "content": "System text"}
    assert converted[1] == {"role": "user", "content": "User prompt"}
    assert converted[2]["role"] == "assistant"
    assert converted[2]["tool_calls"][0]["function"]["name"] == "calc"
    assert converted[3] == {
        "role": "tool",
        "content": "Result 2",
        "tool_call_id": "c_1",
    }


def test_response_to_chat_message_validation() -> None:
    """Verify response conversion rejects empty or invalid responses."""
    with pytest.raises(ValidationError, match="empty response"):
        _response_to_chat_message(None)

    empty_resp = MagicMock()
    empty_resp.choices = []
    with pytest.raises(ValidationError, match="empty response"):
        _response_to_chat_message(empty_resp)


def test_format_triage_prompt(sample_resume: Resume) -> None:
    """Verify format_triage_prompt formats candidate and job info."""
    prompt = format_triage_prompt(
        job_description="Python Senior Engineer",
        resume=sample_resume,
        work_preference="hybrid",
        job_location="Seattle, WA",
        desired_salary_min=150000,
    )
    assert "Python Senior Engineer" in prompt
    assert "hybrid" in prompt
    assert "Seattle, WA" in prompt
    assert "$150,000/year" in prompt


def test_fold_system_into_first_message() -> None:
    """Verify system message is folded into the first user turn."""
    folded = _fold_system_into_first_message(
        "System prompt", [{"role": "user", "content": "User question"}]
    )
    assert len(folded) == 1
    assert "System prompt" in folded[0]["content"]
    assert "User question" in folded[0]["content"]

    folded_empty = _fold_system_into_first_message("System prompt", [])
    assert len(folded_empty) == 1
    assert folded_empty[0] == {"role": "user", "content": "System prompt"}


def test_normalize_and_parse_job_details() -> None:
    """Verify job details parsing and normalization."""
    raw = '{"company": "Acme", "role": "Dev", "location": "Remote", "salary": "100k"}'
    parsed = _parse_job_details_response(raw)
    assert parsed["company"] == "Acme"

    norm = _normalize_job_details({"company": "Acme", "role": None})
    assert norm["company"] == "Acme"
    assert norm["role"] == ""


def test_build_job_details_prompt() -> None:
    """Verify build_job_details_prompt builds the prompt correctly."""
    url = "https://acme.com/jobs/1"
    prompt = _build_job_details_prompt("Job text", "Job Title", url)
    assert "Job text" in prompt
    assert "Job Title" in prompt
    assert f"Source URL:\n```text\n{url}\n```" in prompt


def test_litellm_client_direct_instantiation() -> None:
    """Verify LiteLLMClient resolves models directly."""
    client = LiteLLMClient(
        api_key="key", model_name="models/gemini-2.5-flash", provider="gemini"
    )
    assert client.model_name == "models/gemini-2.5-flash"
    assert client.litellm_model == "gemini/models/gemini-2.5-flash"


def test_triage_result_pydantic_serialization() -> None:
    """Verify TriageResult serialization round-trip with Pydantic model methods."""
    res = TriageResult(
        fit_score=4.5,
        tech_stack_fit=5.0,
        experience_fit=4.0,
        location_fit=4.0,
        salary_fit=4.5,
        industry_fit=4.0,
        reasoning="Solid fit across dimensions.",
    )
    dumped = res.model_dump()
    assert dumped["fit_score"] == 4.5
    assert dumped["reasoning"] == "Solid fit across dimensions."
    reloaded = TriageResult.model_validate(dumped)
    assert reloaded == res


def test_job_details_pydantic_serialization() -> None:
    """Verify JobDetails serialization round-trip with Pydantic model methods."""
    details = JobDetails(
        company="Acme Corp",
        role="Principal Engineer",
        location="Remote",
    )
    dumped = details.model_dump()
    assert dumped["company"] == "Acme Corp"
    reloaded = JobDetails.model_validate(dumped)
    assert reloaded == details
