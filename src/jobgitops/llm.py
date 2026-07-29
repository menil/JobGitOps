"""Pluggable LLM client wrapper supporting Gemini and OpenRouter providers."""

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import yaml

from jobgitops.schema import Resume, ValidationError


@dataclass
class TriageResult:
    """Evaluation result from the job description triage stage."""

    fit_score: float
    tech_stack_fit: float
    experience_fit: float
    location_fit: float
    salary_fit: float
    industry_fit: float
    reasoning: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TriageResult":
        """Parse TriageResult from a dictionary, enforcing bounds and types.

        Raises:
            ValidationError: If any required fields are missing or out of bounds.
        """
        if not isinstance(data, dict):
            raise ValidationError("Triage result must be a dictionary.")

        fit_score = _parse_score_field(data, "fit_score")
        tech_stack_fit = _parse_score_field(data, "tech_stack_fit")
        experience_fit = _parse_score_field(data, "experience_fit")
        location_fit = _parse_score_field(data, "location_fit")
        salary_fit = _parse_score_field(data, "salary_fit")
        industry_fit = _parse_score_field(data, "industry_fit")

        reasoning_val = data.get("reasoning")
        if reasoning_val is None:
            reasoning = ""
        elif isinstance(reasoning_val, bool):
            raise ValidationError("reasoning must be a string, not a boolean.")
        elif isinstance(reasoning_val, (list, dict, set, tuple)):
            raise ValidationError("reasoning must be a string, not a collection.")
        else:
            reasoning = str(reasoning_val)

        return cls(
            fit_score=fit_score,
            tech_stack_fit=tech_stack_fit,
            experience_fit=experience_fit,
            location_fit=location_fit,
            salary_fit=salary_fit,
            industry_fit=industry_fit,
            reasoning=reasoning,
        )


def _parse_score_field(data: dict[str, Any], field_name: str) -> float:
    """Validate and parse a score field from a dictionary."""
    val = data.get(field_name)
    if val is None:
        raise ValidationError(f"Missing required field in triage result: {field_name}")
    if isinstance(val, bool):
        raise ValidationError(f"Field {field_name} must be a number, not a boolean.")
    try:
        score = float(val)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"Field {field_name} must be a number: {e}") from e
    if not (1.0 <= score <= 5.0):
        raise ValidationError(f"Field {field_name} must be between 1.0 and 5.0")
    return score


def clean_json_string(s: str) -> str:
    """Clean markdown formatting and isolate the outermost JSON object."""
    s = s.strip()
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        return s[first : last + 1]
    return s


TRIAGE_PROMPT = (
    "You are an expert technical recruiter triaging a job listing against a "
    "candidate's resume.\nEvaluate the job description against the resume "
    "across 5 granular dimensions, grading each from 1.0 (very poor fit) to "
    "5.0 (perfect fit).\n\n"
    "Candidate Resume (YAML):\n"
    "{resume_yaml}\n\n"
    "Job Description:\n"
    "{job_description}\n\n"
    "Please evaluate the following 5 dimensions:\n"
    "1. Tech Stack Match (match of languages, frameworks, libraries, databases, "
    "and tooling)\n"
    "2. Experience & Years Fit (seniority level, scope of responsibilities, "
    "and years of experience)\n"
    "3. Location & Timezone Suitability (compare remote/onsite and timezone "
    "expectations to candidate preferences/location)\n"
    "4. Salary Alignment (assess if salary matches; if unspecified, grade 5.0 "
    "unless seniority/market fit is poor)\n"
    "5. Industry Domain Familiarity (overlap with domains such as SaaS, "
    "FinTech, DevTools, etc.)\n\n"
    "Calculate the overall `fit_score` as the average or weighted score of "
    "the 5 dimensions.\n\n"
    "Your response MUST be a single JSON object matching the following structure:\n"
    "{{\n"
    '  "fit_score": float,\n'
    '  "tech_stack_fit": float,\n'
    '  "experience_fit": float,\n'
    '  "location_fit": float,\n'
    '  "salary_fit": float,\n'
    '  "industry_fit": float,\n'
    '  "reasoning": "A concise, developer-focused explanation of the '
    'evaluation and any gaps."\n'
    "}}\n\n"
    "Do not return any other text, markdown formatting, or preamble. "
    "Return ONLY the JSON object.\n"
)


TAILOR_PROMPT = (
    "You are an expert resume writer. Subtly adjust the candidate's resume "
    "highlights and skills to align with the provided job description.\n\n"
    "Original Resume (YAML):\n"
    "{resume_yaml}\n\n"
    "Job Description:\n"
    "{job_description}\n\n"
    "Instructions:\n"
    "1. Subtly adjust the resume's `basics.summary`, work `highlights`, and "
    "`skills` keywords to emphasize relevant experience, tools, and "
    "achievements that match the job description.\n"
    "2. CRITICAL CONSTRAINT: Do NOT change, fabricate, or exaggerate dates, "
    "company names, job positions/titles, locations, or educational "
    "degrees/institutions. Keep these exactly identical to the original "
    "resume.\n"
    "3. The output MUST be a valid JSON object matching the JSON Resume "
    "schema structure of the original resume.\n"
    "4. Do not omit any sections present in the original resume (basics, work, "
    "education, skills, projects). Retain them all.\n\n"
    "Return ONLY the completed tailored resume as a JSON object matching the "
    "JSON Resume schema.\n"
    "Do not return any other text, markdown formatting, or preamble. "
    "Return ONLY the JSON object.\n"
)


class LLMClient(ABC):
    """Abstract base class/interface for pluggable LLM client wrappers."""

    @abstractmethod
    def triage_job(self, job_description: str, resume: Resume) -> TriageResult:
        """Evaluate a job description against the resume across 5 dimensions."""
        pass

    @abstractmethod
    def tailor_resume(self, job_description: str, resume: Resume) -> Resume:
        """Subtly adjust resume highlights/skills for the job description."""
        pass


class GeminiClient(LLMClient):
    """LLM client implementation utilizing the official Google Generative AI SDK."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key
        self.model_name = model_name

        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(self.model_name)

    def triage_job(self, job_description: str, resume: Resume) -> TriageResult:
        import google.api_core.exceptions

        resume_yaml = yaml.safe_dump(resume.to_dict(), allow_unicode=True)
        prompt = TRIAGE_PROMPT.format(
            resume_yaml=resume_yaml, job_description=job_description
        )
        try:
            response = self.model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"},
            )
            clean_text = clean_json_string(response.text)
            data = json.loads(clean_text)
            return TriageResult.from_dict(data)
        except (
            json.JSONDecodeError,
            ValidationError,
            google.api_core.exceptions.GoogleAPICallError,
        ) as e:
            raise ValidationError(f"Gemini triage evaluation failed: {e}") from e

    def tailor_resume(self, job_description: str, resume: Resume) -> Resume:
        import google.api_core.exceptions

        resume_yaml = yaml.safe_dump(resume.to_dict(), allow_unicode=True)
        prompt = TAILOR_PROMPT.format(
            resume_yaml=resume_yaml, job_description=job_description
        )
        try:
            response = self.model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"},
            )
            clean_text = clean_json_string(response.text)
            data = json.loads(clean_text)
            return Resume.from_dict(data)
        except (
            json.JSONDecodeError,
            ValidationError,
            google.api_core.exceptions.GoogleAPICallError,
        ) as e:
            raise ValidationError(f"Gemini resume tailoring failed: {e}") from e


class OpenRouterClient(LLMClient):
    """LLM client implementation utilizing standard HTTP requests."""

    def __init__(
        self, api_key: str, model_name: str = "google/gemini-2.5-flash"
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name

    def _call_openrouter(self, prompt: str) -> str:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                if "choices" not in res_data or not res_data["choices"]:
                    raise ValidationError(
                        f"Invalid response format from OpenRouter: {res_data}"
                    )
                return res_data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                error_body = ""
            raise ValidationError(
                f"OpenRouter HTTP Error {e.code}: {e.reason}. Body: {error_body}"
            ) from e
        except urllib.error.URLError as e:
            raise ValidationError(f"OpenRouter Connection Error: {e.reason}") from e
        except TimeoutError as e:
            raise ValidationError(f"OpenRouter Request Timeout: {e}") from e
        except json.JSONDecodeError as e:
            raise ValidationError(f"OpenRouter Invalid JSON Response: {e}") from e
        except ValidationError:
            raise

    def triage_job(self, job_description: str, resume: Resume) -> TriageResult:
        resume_yaml = yaml.safe_dump(resume.to_dict(), allow_unicode=True)
        prompt = TRIAGE_PROMPT.format(
            resume_yaml=resume_yaml, job_description=job_description
        )
        try:
            response_text = self._call_openrouter(prompt)
            clean_text = clean_json_string(response_text)
            data = json.loads(clean_text)
            return TriageResult.from_dict(data)
        except (json.JSONDecodeError, ValidationError) as e:
            raise ValidationError(f"OpenRouter triage evaluation failed: {e}") from e

    def tailor_resume(self, job_description: str, resume: Resume) -> Resume:
        resume_yaml = yaml.safe_dump(resume.to_dict(), allow_unicode=True)
        prompt = TAILOR_PROMPT.format(
            resume_yaml=resume_yaml, job_description=job_description
        )
        try:
            response_text = self._call_openrouter(prompt)
            clean_text = clean_json_string(response_text)
            data = json.loads(clean_text)
            return Resume.from_dict(data)
        except (json.JSONDecodeError, ValidationError) as e:
            raise ValidationError(f"OpenRouter resume tailoring failed: {e}") from e


def get_llm_client() -> LLMClient:
    """Instantiate pluggable LLM client based on environment variables."""
    provider = os.environ.get("LLM_PROVIDER")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")

    if not provider:
        if gemini_key:
            provider = "gemini"
        elif openrouter_key:
            provider = "openrouter"
        else:
            raise ValidationError(
                "No LLM provider configured. Please set GEMINI_API_KEY or "
                "OPENROUTER_API_KEY."
            )

    provider = provider.lower()
    if provider == "gemini":
        if not gemini_key:
            raise ValidationError("GEMINI_API_KEY environment variable is missing.")
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        return GeminiClient(api_key=gemini_key, model_name=model_name)
    elif provider == "openrouter":
        if not openrouter_key:
            raise ValidationError("OPENROUTER_API_KEY environment variable is missing.")
        model_name = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
        return OpenRouterClient(api_key=openrouter_key, model_name=model_name)
    else:
        raise ValidationError(f"Unknown LLM provider specified: {provider}")
