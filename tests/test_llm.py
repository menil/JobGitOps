"""Unit tests for the LLM wrapper client and prompt parsing logic."""

import json
import os
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import google.api_core.exceptions
import pytest

from jobgitops.llm import (
    GeminiClient,
    OpenRouterClient,
    TriageResult,
    clean_json_string,
    get_llm_client,
)
from jobgitops.schema import Resume, ValidationError


@pytest.fixture
def sample_resume() -> Resume:
    """Provide a minimal parsed Resume instance for LLM input testing."""
    return Resume.from_dict(
        {
            "basics": {
                "name": "Martin Livne",
                "email": "martin@example.com",
                "summary": "Experienced python developer.",
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


@patch.dict(os.environ, {}, clear=True)
def test_get_llm_client_missing_config() -> None:
    """Verify get_llm_client raises ValidationError when no credentials exist."""
    with pytest.raises(ValidationError, match="No LLM provider configured"):
        get_llm_client()


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=True)
@patch("google.generativeai.configure")
def test_get_llm_client_default_gemini(mock_configure: MagicMock) -> None:
    """Verify Gemini is selected by default when only GEMINI_API_KEY is present."""
    client = get_llm_client()
    assert isinstance(client, GeminiClient)
    assert client.api_key == "fake-gemini-key"
    assert client.model_name == "models/gemini-2.5-flash"
    mock_configure.assert_called_once_with(api_key="fake-gemini-key")


@patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-or-key"}, clear=True)
def test_get_llm_client_default_openrouter() -> None:
    """Verify OpenRouter is selected by default when key is present."""
    client = get_llm_client()
    assert isinstance(client, OpenRouterClient)
    assert client.api_key == "fake-or-key"
    assert client.model_name == "google/gemini-2.5-flash"


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


@patch.dict(
    os.environ,
    {
        "GEMINI_API_KEY": "custom-key",
        "LLM_PROVIDER": "Gemini",
        "GEMINI_MODEL": "gemini-pro-custom",
    },
    clear=True,
)
@patch("google.generativeai.configure")
def test_get_llm_client_custom_gemini_model(mock_configure: MagicMock) -> None:
    """Verify custom Gemini model can be configured through environment variables."""
    client = get_llm_client()
    assert isinstance(client, GeminiClient)
    assert client.model_name == "gemini-pro-custom"


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


@patch.dict(
    os.environ,
    {"LLM_PROVIDER": "invalid-provider", "GEMINI_API_KEY": "key"},
    clear=True,
)
def test_get_llm_client_invalid_provider() -> None:
    """Verify invalid provider string raises ValidationError."""
    with pytest.raises(ValidationError, match="Unknown LLM provider specified"):
        get_llm_client()


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_triage(
    mock_configure: MagicMock, mock_model_cls: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient generates triage results successfully."""
    mock_model = mock_model_cls.return_value
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "fit_score": 4.0,
            "tech_stack_fit": 4.0,
            "experience_fit": 4.0,
            "location_fit": 4.0,
            "salary_fit": 4.0,
            "industry_fit": 4.0,
            "reasoning": "Standard match",
        }
    )
    mock_model.generate_content.return_value = mock_response

    client = GeminiClient(api_key="key")
    res = client.triage_job("Python role in Seattle", sample_resume)

    assert res.fit_score == 4.0
    assert res.reasoning == "Standard match"
    mock_model.generate_content.assert_called_once()


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_tailor(
    mock_configure: MagicMock, mock_model_cls: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient returns a valid, updated Resume object after tailoring."""
    mock_model = mock_model_cls.return_value
    mock_response = MagicMock()
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Highly tailored Python profile."
    mock_response.text = json.dumps(tailored_data)
    mock_model.generate_content.return_value = mock_response

    client = GeminiClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.summary == "Highly tailored Python profile."
    assert tailored_resume.basics.name == "Martin Livne"


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_failure_handling(
    mock_configure: MagicMock, mock_model_cls: MagicMock, sample_resume: Resume
) -> None:
    """Verify GeminiClient exceptions are caught and raised as ValidationError."""
    mock_model = mock_model_cls.return_value
    mock_model.generate_content.side_effect = (
        google.api_core.exceptions.InternalServerError("API error")
    )

    client = GeminiClient(api_key="key")

    with pytest.raises(ValidationError, match="Gemini triage evaluation failed"):
        client.triage_job("Python role", sample_resume)

    with pytest.raises(ValidationError, match="Gemini resume tailoring failed"):
        client.tailor_resume("Python role", sample_resume)


@patch("urllib.request.urlopen")
def test_openrouter_client_triage(
    mock_urlopen: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouterClient performs triage successfully via HTTP mock."""
    mock_response = MagicMock()
    triage_payload = {
        "fit_score": 4.2,
        "tech_stack_fit": 4.5,
        "experience_fit": 4.0,
        "location_fit": 4.0,
        "salary_fit": 4.0,
        "industry_fit": 4.5,
        "reasoning": "Matches stack preferences.",
    }
    mock_response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": json.dumps(triage_payload)}}]}
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response

    client = OpenRouterClient(api_key="key")
    res = client.triage_job("Python engineer role", sample_resume)

    assert res.fit_score == 4.2
    assert res.reasoning == "Matches stack preferences."
    mock_urlopen.assert_called_once()

    called_req = mock_urlopen.call_args[0][0]
    assert isinstance(called_req, urllib.request.Request)
    assert called_req.get_header("Authorization") == "Bearer key"
    assert called_req.get_header("Content-type") == "application/json"
    assert called_req.full_url == "https://openrouter.ai/api/v1/chat/completions"


@patch("urllib.request.urlopen")
def test_openrouter_client_tailor(
    mock_urlopen: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouterClient returns a correctly-parsed Resume after tailoring."""
    mock_response = MagicMock()
    tailored_data = sample_resume.to_dict()
    tailored_data["basics"]["summary"] = "Tailored via OpenRouter."
    mock_response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": json.dumps(tailored_data)}}]}
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response

    client = OpenRouterClient(api_key="key")
    tailored_resume = client.tailor_resume("Python role", sample_resume)

    assert tailored_resume.basics.summary == "Tailored via OpenRouter."


@patch("urllib.request.urlopen")
def test_openrouter_client_failure_handling(
    mock_urlopen: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter HTTP issues or parsing errors raise ValidationError."""
    mock_urlopen.side_effect = urllib.error.URLError("Connection reset")

    client = OpenRouterClient(api_key="key")

    with pytest.raises(ValidationError, match="OpenRouter Connection Error"):
        client.triage_job("Python role", sample_resume)

    mock_urlopen.side_effect = None
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"status": "ok"}'
    mock_urlopen.return_value.__enter__.return_value = mock_response

    msg = "Invalid response format from OpenRouter"
    with pytest.raises(ValidationError, match=msg):
        client.triage_job("Python role", sample_resume)


@patch("urllib.request.urlopen")
def test_openrouter_client_http_error_handling(
    mock_urlopen: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter client extracts details from HTTPError objects."""
    mock_response = MagicMock()
    mock_response.read.return_value = b"Forbidden resource access"
    http_error = urllib.error.HTTPError(
        url="https://openrouter.ai",
        code=403,
        msg="Forbidden",
        hdrs=None,  # type: ignore
        fp=mock_response,
    )
    mock_urlopen.side_effect = http_error

    client = OpenRouterClient(api_key="key")

    msg = "OpenRouter HTTP Error 403: Forbidden. Body: Forbidden resource access"
    with pytest.raises(ValidationError, match=msg):
        client.triage_job("Python role", sample_resume)


@patch("urllib.request.urlopen")
def test_openrouter_client_malformed_json_response(
    mock_urlopen: MagicMock, sample_resume: Resume
) -> None:
    """Verify OpenRouter client wraps JSON decode errors in ValidationError."""
    mock_response = MagicMock()
    mock_response.read.return_value = b"invalid json content"
    mock_urlopen.return_value.__enter__.return_value = mock_response

    client = OpenRouterClient(api_key="key")

    with pytest.raises(ValidationError, match="OpenRouter triage evaluation failed"):
        client.triage_job("Python role", sample_resume)

    with pytest.raises(ValidationError, match="OpenRouter resume tailoring failed"):
        client.tailor_resume("Python role", sample_resume)
