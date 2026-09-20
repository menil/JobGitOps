"""Data schemas and dataclasses for JobGitOps."""

import datetime
import re
from typing import Any, ClassVar

import pydantic
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from jobgitops.fit_grades import FIT_GRADE_B_MIN


class ValidationError(ValueError):
    """Raised when configuration or resume data validation fails."""

    pass


def _parse_str(field_name: str, val: Any) -> str | None:
    """Parse a value into a string with ISO formatting for dates.

    Args:
        field_name: The name of the field being validated.
        val: The value to validate.

    Returns:
        The string representation of the value, or None if input was None.

    Raises:
        ValidationError: If the value is a boolean or a collection.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        raise ValidationError(f"{field_name} must be a string, not a boolean.")
    if isinstance(val, (list, dict, set, tuple)):
        raise ValidationError(f"{field_name} must be a string, not a collection.")
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()
    return str(val)


def _parse_int(field_name: str, val: Any, default: int) -> int:
    """Parse a value into an int, rejecting booleans.

    Args:
        field_name: The name of the field being validated.
        val: The value to validate, or None to use the default.
        default: Default value when val is None.

    Returns:
        The integer value.

    Raises:
        ValidationError: If the value cannot be coerced to an int.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        raise ValidationError(f"{field_name} must be an integer.")
    try:
        return int(val)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"{field_name} must be an integer.") from e


def _parse_positive_int(field_name: str, val: Any, default: int) -> int:
    """Parse a value into a positive int.

    Args:
        field_name: The name of the field being validated.
        val: The value to validate, or None to use the default.
        default: Default value when val is None.

    Returns:
        The positive integer value.

    Raises:
        ValidationError: If the value is not a positive int.
    """
    parsed = _parse_int(field_name, val, default)
    if parsed <= 0:
        raise ValidationError(f"{field_name} must be greater than zero.")
    return parsed


def _parse_float(field_name: str, val: Any, default: float) -> float:
    """Parse a value into a float, rejecting booleans.

    Args:
        field_name: The name of the field being validated.
        val: The value to validate, or None to use the default.
        default: Default value when val is None.

    Returns:
        The float value.

    Raises:
        ValidationError: If the value cannot be coerced to a float.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        raise ValidationError(f"{field_name} must be a number.")
    try:
        return float(val)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"{field_name} must be a number.") from e


def _parse_bool(field_name: str, val: Any, default: bool) -> bool:
    """Parse a value into a bool, requiring an actual boolean.

    Args:
        field_name: The name of the field being validated.
        val: The value to validate, or None to use the default.
        default: Default value when val is None.

    Returns:
        The boolean value.

    Raises:
        ValidationError: If the value is not a boolean.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    raise ValidationError(f"{field_name} must be a boolean.")


class SearchConfig(BaseModel):
    """Job search scraper configuration."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    work_preference: str = "hybrid"
    job_type: str = "fulltime"
    platforms: list[str] = Field(
        default_factory=lambda: ["linkedin", "indeed", "zip_recruiter"]
    )
    hours_old: int = 24
    enabled: bool = True
    desired_salary_min: int | None = None

    @field_validator("work_preference", mode="before")
    @classmethod
    def _validate_work_preference(cls, v: Any) -> str:
        if v is None:
            return "hybrid"
        if isinstance(v, bool) or not isinstance(v, str):
            raise ValidationError(
                "search.work_preference must be a string, not a collection."
                if isinstance(v, (list, dict, set, tuple))
                else "search.work_preference must be a string, not a boolean."
            )
        v_clean = v.lower().strip()
        if v_clean not in ("remote", "onsite", "hybrid"):
            raise ValidationError(
                "search.work_preference must be one of: remote, onsite, hybrid."
            )
        return v_clean

    @field_validator("job_type", mode="before")
    @classmethod
    def _validate_job_type(cls, v: Any) -> str:
        if v is None:
            return "fulltime"
        if isinstance(v, bool) or not isinstance(v, str):
            raise ValidationError("search.job_type must be a string.")
        return v

    @field_validator("platforms", mode="before")
    @classmethod
    def _validate_platforms(cls, v: Any) -> list[str]:
        if v is None:
            return ["linkedin", "indeed", "zip_recruiter"]
        if not isinstance(v, list) or not all(
            isinstance(p, str) and not isinstance(p, bool) for p in v
        ):
            raise ValidationError("search.platforms must be a list of strings.")
        return v

    @field_validator("hours_old", mode="before")
    @classmethod
    def _validate_hours_old(cls, v: Any) -> int:
        if v is None:
            return 24
        if isinstance(v, bool):
            raise ValidationError("search.hours_old must be an integer.")
        try:
            val = int(v)
        except (ValueError, TypeError) as e:
            raise ValidationError("search.hours_old must be an integer.") from e
        if val <= 0:
            raise ValidationError("search.hours_old must be greater than zero.")
        return val

    @field_validator("desired_salary_min", mode="before")
    @classmethod
    def _validate_desired_salary_min(cls, v: Any) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValidationError("search.desired_salary_min must be an integer.")
        try:
            val = int(v)
        except (ValueError, TypeError) as e:
            raise ValidationError(
                "search.desired_salary_min must be an integer."
            ) from e
        if val <= 0:
            raise ValidationError(
                "search.desired_salary_min must be greater than zero."
            )
        return val

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return v.lower() not in ("false", "0", "no", "")
        return bool(v)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchConfig":
        """Parse search configuration from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("Search configuration must be a dictionary.")
        try:
            return cls.model_validate(data)
        except pydantic.ValidationError as e:
            first_err = e.errors()[0]
            msg = first_err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            raise ValidationError(f"Failed to parse SearchConfig: {msg}") from e


class ProjectsV2Config(BaseModel):
    """Optional GitHub Projects V2 automation configuration."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    project_id: str
    status_field_name: str = "Status"
    PLACEHOLDER_PREFIX: ClassVar[str] = "PVT_YOUR_"

    @field_validator("project_id", mode="before")
    @classmethod
    def _validate_project_id(cls, v: Any) -> str:
        if v is None or isinstance(v, bool) or not isinstance(v, str) or not v.strip():
            raise ValidationError("projects_v2.project_id must be a non-empty string.")
        if v.startswith(cls.PLACEHOLDER_PREFIX):
            return ""
        return v

    @field_validator("status_field_name", mode="before")
    @classmethod
    def _validate_status_field_name(cls, v: Any) -> str:
        if v is None:
            return "Status"
        if isinstance(v, bool) or not isinstance(v, str):
            raise ValidationError("projects_v2.status_field_name must be a string.")
        return v or "Status"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectsV2Config":
        """Parse Projects V2 configuration from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("projects_v2 configuration must be a dictionary.")
        try:
            return cls.model_validate(data)
        except pydantic.ValidationError as e:
            first_err = e.errors()[0]
            msg = first_err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            raise ValidationError(f"Failed to parse ProjectsV2Config: {msg}") from e


# Maximum decompressed response body accepted from a fetched page (1 MiB).
# Sized for JS-heavy job boards (e.g. LinkedIn serves ~300 KiB of HTML) while
# still bounding memory on the runner.
MAX_CONTENT_BYTES = 1048576

# Positive-int research fields parsed with the same shared helper.
_RESEARCH_INT_FIELDS: tuple[str, ...] = (
    "max_results",
    "max_iterations",
    "max_context_comments",
    "timeout_seconds",
    "total_timeout_seconds",
    "max_redirects",
    "max_content_bytes",
    "max_jina_calls",
)

# Boolean research fields parsed with the same shared helper.
_RESEARCH_BOOL_FIELDS: tuple[str, ...] = ("use_jina_reader", "block_private_ips")


class ResearchConfig(BaseModel):
    """Issue Assistant research / web-tool configuration."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    search_provider: str = "duckduckgo"
    max_results: int = 5
    max_iterations: int = 6
    max_context_comments: int = 10
    timeout_seconds: int = 15
    total_timeout_seconds: int = 30
    max_redirects: int = 5
    max_content_bytes: int = MAX_CONTENT_BYTES
    request_delay: float = 1.0
    use_jina_reader: bool = True
    max_jina_calls: int = 5
    block_private_ips: bool = True
    model: str = ""

    @field_validator("request_delay", mode="before")
    @classmethod
    def _validate_request_delay(cls, v: Any) -> float:
        if v is None:
            return 1.0
        if isinstance(v, bool):
            raise ValidationError("research.request_delay must be a number.")
        try:
            val = float(v)
        except (ValueError, TypeError) as e:
            raise ValidationError("research.request_delay must be a number.") from e
        if val < 0:
            raise ValidationError("research.request_delay must not be negative.")
        return val

    @field_validator(
        "max_results",
        "max_iterations",
        "max_context_comments",
        "timeout_seconds",
        "total_timeout_seconds",
        "max_redirects",
        "max_content_bytes",
        mode="before",
    )
    @classmethod
    def _validate_positive_ints(cls, v: Any, info: ValidationInfo) -> int:
        field_name = info.field_name
        if v is None:
            default_val = cls.model_fields[field_name].default
            return default_val if isinstance(default_val, int) else 1
        if isinstance(v, bool):
            raise ValidationError(f"research.{field_name} must be an integer.")
        try:
            val = int(v)
        except (ValueError, TypeError) as e:
            raise ValidationError(f"research.{field_name} must be an integer.") from e
        if val <= 0:
            raise ValidationError(f"research.{field_name} must be greater than zero.")
        return val

    @field_validator("max_jina_calls", mode="before")
    @classmethod
    def _validate_max_jina_calls(cls, v: Any, info: ValidationInfo) -> int:
        if v is None:
            return 5
        if isinstance(v, bool):
            raise ValidationError("research.max_jina_calls must be an integer.")
        try:
            val = int(v)
        except (ValueError, TypeError) as e:
            raise ValidationError("research.max_jina_calls must be an integer.") from e
        is_from_dict = info.context and info.context.get("from_dict")
        if is_from_dict and val <= 0:
            raise ValidationError("research.max_jina_calls must be greater than zero.")
        if val < 0:
            raise ValidationError("research.max_jina_calls must not be negative.")
        return val

    @field_validator("use_jina_reader", "block_private_ips", mode="before")
    @classmethod
    def _validate_bools(cls, v: Any, info: ValidationInfo) -> bool:
        field_name = info.field_name
        if v is None:
            return True
        if isinstance(v, bool):
            return v
        raise ValidationError(f"research.{field_name} must be a boolean.")

    @field_validator("search_provider", "model", mode="before")
    @classmethod
    def _validate_str_fields(cls, v: Any, info: ValidationInfo) -> str:
        field_name = info.field_name
        default_val = "duckduckgo" if field_name == "search_provider" else ""
        if v is None:
            return default_val
        if isinstance(v, bool):
            raise ValidationError(
                f"research.{field_name} must be a string, not a boolean."
            )
        if not isinstance(v, str):
            raise ValidationError(f"research.{field_name} must be a string.")
        return v or default_val

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchConfig":
        """Parse research configuration from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("Research configuration must be a dictionary.")
        try:
            return cls.model_validate(data, context={"from_dict": True})
        except pydantic.ValidationError as e:
            first_err = e.errors()[0]
            msg = first_err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            raise ValidationError(f"Failed to parse ResearchConfig: {msg}") from e


_GITHUB_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NPM_PINNED_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def theme_looks_pinned(theme_spec: str) -> bool:
    """Check theme_spec is pinned to an exact version or 40-char commit SHA."""
    if theme_spec.startswith("github:"):
        _, _, ref = theme_spec.partition("#")
        return bool(_GITHUB_COMMIT_SHA_RE.fullmatch(ref))
    name, sep, version = theme_spec.rpartition("@")
    return bool(sep) and bool(name) and bool(_NPM_PINNED_VERSION_RE.fullmatch(version))


class Settings(BaseModel):
    """App-wide settings loaded from config/settings.yaml."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    fit_threshold: float = FIT_GRADE_B_MIN
    search: SearchConfig | Any = Field(default_factory=SearchConfig)
    custom_queries: list[str] | None = None
    projects_v2: ProjectsV2Config | Any = None
    research: ResearchConfig | Any = Field(default_factory=ResearchConfig)
    theme: str | None = None

    @field_validator("fit_threshold", mode="before")
    @classmethod
    def _validate_fit_threshold(cls, v: Any) -> float:
        if v is None:
            return FIT_GRADE_B_MIN
        if isinstance(v, bool):
            raise ValidationError("fit_threshold must be a number.")
        try:
            val = float(v)
        except (ValueError, TypeError) as e:
            raise ValidationError("fit_threshold must be a number.") from e
        if not (1.0 <= val <= 5.0):
            raise ValidationError("fit_threshold must be between 1.0 and 5.0.")
        return val

    @field_validator("custom_queries", mode="before")
    @classmethod
    def _validate_custom_queries(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if not isinstance(v, list) or not all(
            isinstance(q, str) and not isinstance(q, bool) for q in v
        ):
            raise ValidationError("custom_queries must be a list of strings.")
        return v

    @field_validator("theme", mode="before")
    @classmethod
    def _validate_theme(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValidationError("theme must be a string, not a boolean.")
        if isinstance(v, (list, dict, set, tuple)):
            raise ValidationError("theme must be a string, not a collection.")
        if not isinstance(v, str):
            raise ValidationError("theme must be a string.")
        if not theme_looks_pinned(v):
            raise ValidationError(
                "theme must be pinned to an exact version or commit SHA: "
                '"<package>@<version>" or "github:<owner>/<repo>#<40-char-sha>" '
                f"(got {v!r}). Omit the key entirely to use the default."
            )
        return v

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Parse settings from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("Settings data must be a dictionary.")
        if (
            "search" in data
            and not isinstance(data["search"], dict)
            and data["search"] is not None
        ):
            raise ValidationError("Search configuration must be a dictionary.")
        if (
            "projects_v2" in data
            and not isinstance(data["projects_v2"], dict)
            and data["projects_v2"] is not None
        ):
            raise ValidationError("projects_v2 configuration must be a dictionary.")
        if (
            "research" in data
            and not isinstance(data["research"], dict)
            and data["research"] is not None
        ):
            raise ValidationError("Research configuration must be a dictionary.")

        try:
            search_data = data.get("search") or {}
            search = (
                SearchConfig.from_dict(search_data)
                if isinstance(search_data, dict)
                else SearchConfig.model_validate(search_data)
            )

            projects_v2_data = data.get("projects_v2")
            projects_v2 = (
                ProjectsV2Config.from_dict(projects_v2_data)
                if projects_v2_data is not None
                else None
            )

            research_data = data.get("research") or {}
            research = (
                ResearchConfig.from_dict(research_data)
                if isinstance(research_data, dict)
                else ResearchConfig.model_validate(research_data)
            )

            payload = {
                **data,
                "search": search,
                "projects_v2": projects_v2,
                "research": research,
            }
            return cls.model_validate(payload)
        except pydantic.ValidationError as e:
            first_err = e.errors()[0]
            msg = first_err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            raise ValidationError(f"Failed to parse Settings: {msg}") from e
        except ValidationError:
            raise


# --- JSON Resume Schema Pydantic Models ---


class Location(BaseModel):
    """Location information for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    city: str | None = None
    state: str | None = None
    country_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Location":
        """Parse location details from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("basics.location must be a dictionary.")

        state_val = data.get("state") or data.get("region")
        country_val = data.get("countryCode") or data.get("country_code")

        city = _parse_str("basics.location.city", data.get("city"))
        state = _parse_str("basics.location.state", state_val)
        country_code = _parse_str("basics.location.country_code", country_val)

        if not city:
            raise ValidationError("basics.location.city is required in resume.")
        if not country_code:
            raise ValidationError("basics.location.country_code is required in resume.")

        return cls(
            city=city,
            state=state,
            country_code=country_code,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Location to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {}
        if self.city is not None:
            res["city"] = self.city
        if self.state is not None:
            res["region"] = self.state
        if self.country_code is not None:
            res["countryCode"] = self.country_code
        return res


class Profile(BaseModel):
    """Social profiles (GitHub, LinkedIn) for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    network: str
    username: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        """Parse social profile details from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("profile details must be a dictionary.")

        network = _parse_str("profile.network", data.get("network"))
        if not network:
            raise ValidationError("profile.network must be a non-empty string.")

        return cls(
            network=network,
            username=_parse_str("profile.username", data.get("username")),
            url=_parse_str("profile.url", data.get("url")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Profile to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {
            "network": self.network,
        }
        if self.username is not None:
            res["username"] = self.username
        if self.url is not None:
            res["url"] = self.url
        return res


def _set_array(
    res: dict[str, Any], key: str, items: list[Any], *, include_empty_arrays: bool
) -> None:
    """Set ``res[key] = items`` unless items is empty and include_empty_arrays is False.

    Shared by every to_dict() below to keep the emit-vs-omit rule for optional
    array fields (profiles, highlights, courses, keywords, work, education,
    skills, projects) in one place -- see Basics.to_dict's docstring for why
    the rule exists.
    """
    if items or include_empty_arrays:
        res[key] = items


class Basics(BaseModel):
    """Basic profile information for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    name: str
    label: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
    summary: str | None = None
    location: Location | None = None
    profiles: list[Profile] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Basics":
        """Parse basics section from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("Resume basics must be a dictionary.")

        try:
            name = _parse_str("basics.name", data.get("name"))
            if not name:
                raise ValidationError("basics.name is required and must be a string.")

            location_data = data.get("location")
            location = Location.from_dict(location_data) if location_data else None

            profiles_data = data.get("profiles")
            if profiles_data is None:
                profiles_data = []
            if not isinstance(profiles_data, list):
                raise ValidationError("basics.profiles must be a list.")
            profiles = [Profile.from_dict(p) for p in profiles_data]

            return cls(
                name=name,
                label=_parse_str("basics.label", data.get("label")),
                email=_parse_str("basics.email", data.get("email")),
                phone=_parse_str("basics.phone", data.get("phone")),
                url=_parse_str("basics.url", data.get("url")),
                summary=_parse_str("basics.summary", data.get("summary")),
                location=location,
                profiles=profiles,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Basics: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Basics to dictionary conforming to JSON Resume schema.

        Args:
            include_empty_arrays: When True (the default, used for the
                theme-facing resume.json), always emit `profiles` even when
                empty -- real JSON Resume themes (e.g.
                @jsonresume/jsonresume-theme-professional) assume this key
                exists and crash on `basics.profiles.find(...)` when it's
                absent entirely, confirmed by hands-on testing while
                building JobGitOps-184. When False (used for the canonical
                resumes/resume.yaml), omit it when empty instead, so an
                author who never wrote `profiles` isn't forced to -- see
                loader.py's render_resume_yaml.
        """
        res: dict[str, Any] = {
            "name": self.name,
        }
        if self.label is not None:
            res["label"] = self.label
        if self.email is not None:
            res["email"] = self.email
        if self.phone is not None:
            res["phone"] = self.phone
        if self.url is not None:
            res["url"] = self.url
        if self.summary is not None:
            res["summary"] = self.summary
        if self.location is not None:
            res["location"] = (
                self.location.to_dict()
                if hasattr(self.location, "to_dict")
                else self.location
            )
        _set_array(
            res,
            "profiles",
            [p.to_dict() if hasattr(p, "to_dict") else p for p in self.profiles],
            include_empty_arrays=include_empty_arrays,
        )
        return res


class Work(BaseModel):
    """Professional work experience entry for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    name: str
    position: str
    url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Work":
        """Parse work experience entry from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("work entry must be a dictionary.")

        try:
            name = _parse_str("work.name", data.get("name"))
            if not name:
                raise ValidationError("work.name is required and must be a string.")

            position = _parse_str("work.position", data.get("position"))
            if not position:
                raise ValidationError("work.position is required and must be a string.")

            highlights_raw = data.get("highlights")
            if highlights_raw is None:
                highlights_raw = []
            if not isinstance(highlights_raw, list) or not all(
                isinstance(h, str) and not isinstance(h, bool) for h in highlights_raw
            ):
                raise ValidationError("work.highlights must be a list of strings.")
            highlights = highlights_raw

            return cls(
                name=name,
                position=position,
                url=_parse_str("work.url", data.get("url")),
                start_date=_parse_str("work.start_date", data.get("startDate")),
                end_date=_parse_str("work.end_date", data.get("endDate")),
                summary=_parse_str("work.summary", data.get("summary")),
                highlights=highlights,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Work: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Work to dictionary conforming to JSON Resume schema.

        See Basics.to_dict's `include_empty_arrays` docstring.
        """
        res: dict[str, Any] = {
            "name": self.name,
            "position": self.position,
        }
        if self.url is not None:
            res["url"] = self.url
        if self.start_date is not None:
            res["startDate"] = self.start_date
        if self.end_date is not None:
            res["endDate"] = self.end_date
        if self.summary is not None:
            res["summary"] = self.summary
        _set_array(
            res,
            "highlights",
            self.highlights,
            include_empty_arrays=include_empty_arrays,
        )
        return res


class Education(BaseModel):
    """Education history entry for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    institution: str
    url: str | None = None
    area: str | None = None
    study_type: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    score: str | None = None
    courses: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Education":
        """Parse education history entry from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("education entry must be a dictionary.")

        try:
            institution = _parse_str("education.institution", data.get("institution"))
            if not institution:
                raise ValidationError(
                    "education.institution is required and must be a string."
                )

            courses_raw = data.get("courses")
            if courses_raw is None:
                courses_raw = []
            if not isinstance(courses_raw, list) or not all(
                isinstance(c, str) and not isinstance(c, bool) for c in courses_raw
            ):
                raise ValidationError("education.courses must be a list of strings.")
            courses = courses_raw

            return cls(
                institution=institution,
                url=_parse_str("education.url", data.get("url")),
                area=_parse_str("education.area", data.get("area")),
                study_type=_parse_str("education.study_type", data.get("studyType")),
                start_date=_parse_str("education.start_date", data.get("startDate")),
                end_date=_parse_str("education.end_date", data.get("endDate")),
                score=_parse_str("education.score", data.get("score")),
                courses=courses,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Education: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Education to dictionary conforming to JSON Resume schema.

        See Basics.to_dict's `include_empty_arrays` docstring.
        """
        res: dict[str, Any] = {
            "institution": self.institution,
        }
        if self.url is not None:
            res["url"] = self.url
        if self.area is not None:
            res["area"] = self.area
        if self.study_type is not None:
            res["studyType"] = self.study_type
        if self.start_date is not None:
            res["startDate"] = self.start_date
        if self.end_date is not None:
            res["endDate"] = self.end_date
        if self.score is not None:
            res["score"] = self.score
        _set_array(
            res, "courses", self.courses, include_empty_arrays=include_empty_arrays
        )
        return res


class Skill(BaseModel):
    """Professional skills entry for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    name: str
    keywords: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        """Parse skill entry from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("skill entry must be a dictionary.")

        try:
            name = _parse_str("skill.name", data.get("name"))
            if not name:
                raise ValidationError("skill.name is required and must be a string.")

            keywords_raw = data.get("keywords")
            if keywords_raw is None:
                keywords_raw = []
            if not isinstance(keywords_raw, list) or not all(
                isinstance(k, str) and not isinstance(k, bool) for k in keywords_raw
            ):
                raise ValidationError("skill.keywords must be a list of strings.")
            keywords = keywords_raw

            return cls(name=name, keywords=keywords)
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Skill: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Skill to dictionary conforming to JSON Resume schema.

        See Basics.to_dict's `include_empty_arrays` docstring.
        """
        res: dict[str, Any] = {
            "name": self.name,
        }
        _set_array(
            res, "keywords", self.keywords, include_empty_arrays=include_empty_arrays
        )
        return res


class Project(BaseModel):
    """Personal or professional project entry for JSON Resume."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    name: str
    description: str | None = None
    highlights: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        """Parse project entry from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("project entry must be a dictionary.")

        try:
            name = _parse_str("project.name", data.get("name"))
            if not name:
                raise ValidationError("project.name is required and must be a string.")

            highlights_raw = data.get("highlights")
            if highlights_raw is None:
                highlights_raw = []
            if not isinstance(highlights_raw, list) or not all(
                isinstance(h, str) and not isinstance(h, bool) for h in highlights_raw
            ):
                raise ValidationError("project.highlights must be a list of strings.")
            highlights = highlights_raw

            keywords_raw = data.get("keywords")
            if keywords_raw is None:
                keywords_raw = []
            if not isinstance(keywords_raw, list) or not all(
                isinstance(k, str) and not isinstance(k, bool) for k in keywords_raw
            ):
                raise ValidationError("project.keywords must be a list of strings.")
            keywords = keywords_raw

            return cls(
                name=name,
                description=_parse_str("project.description", data.get("description")),
                highlights=highlights,
                keywords=keywords,
                start_date=_parse_str("project.start_date", data.get("startDate")),
                end_date=_parse_str("project.end_date", data.get("endDate")),
                url=_parse_str("project.url", data.get("url")),
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Project: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Project to dictionary conforming to JSON Resume schema.

        See Basics.to_dict's `include_empty_arrays` docstring.
        """
        res: dict[str, Any] = {
            "name": self.name,
        }
        if self.description is not None:
            res["description"] = self.description
        _set_array(
            res,
            "highlights",
            self.highlights,
            include_empty_arrays=include_empty_arrays,
        )
        _set_array(
            res, "keywords", self.keywords, include_empty_arrays=include_empty_arrays
        )
        if self.start_date is not None:
            res["startDate"] = self.start_date
        if self.end_date is not None:
            res["endDate"] = self.end_date
        if self.url is not None:
            res["url"] = self.url
        return res


class Resume(BaseModel):
    """Full resume conforming to JSON Resume schema conventions."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    basics: Basics
    work: list[Work] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    meta: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resume":
        """Parse full resume from dictionary with parsing validation."""
        if not isinstance(data, dict):
            raise ValidationError("Resume data must be a dictionary.")

        try:
            basics_data = data.get("basics")
            if basics_data is None:
                raise ValidationError("basics section is required in resume.")
            if not isinstance(basics_data, dict):
                raise ValidationError("basics section must be a dictionary.")
            basics = Basics.from_dict(basics_data)

            work_data = data.get("work")
            if work_data is None:
                work_data = []
            if not isinstance(work_data, list):
                raise ValidationError("work section must be a list.")
            work = [Work.from_dict(w) for w in work_data]

            education_data = data.get("education")
            if education_data is None:
                education_data = []
            if not isinstance(education_data, list):
                raise ValidationError("education section must be a list.")
            education = [Education.from_dict(e) for e in education_data]

            skills_data = data.get("skills")
            if skills_data is None:
                skills_data = []
            if not isinstance(skills_data, list):
                raise ValidationError("skills section must be a list.")
            skills = [Skill.from_dict(s) for s in skills_data]

            projects_data = data.get("projects")
            if projects_data is None:
                projects_data = []
            if not isinstance(projects_data, list):
                raise ValidationError("projects section must be a list.")
            projects = [Project.from_dict(p) for p in projects_data]

            meta_data = data.get("meta")
            if meta_data is not None and not isinstance(meta_data, dict):
                raise ValidationError("meta section must be a dictionary.")
            meta = meta_data

            return cls(
                basics=basics,
                work=work,
                education=education,
                skills=skills,
                projects=projects,
                meta=meta,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Resume: {e}") from e

    def to_dict(self, *, include_empty_arrays: bool = True) -> dict[str, Any]:
        """Convert Resume to dictionary conforming to JSON Resume schema.

        Args:
            include_empty_arrays: See Basics.to_dict's docstring; threaded
                through to every nested Work/Education/Skill/Project, and
                also governs whether `work`/`education`/`skills`/`projects`
                themselves are emitted when empty. renderer.py's
                compile_resume_json uses the default (True) for the
                theme-facing resume.json; loader.py's render_resume_yaml
                passes False for the canonical resumes/resume.yaml.
        """
        res: dict[str, Any] = {
            "basics": self.basics.to_dict(include_empty_arrays=include_empty_arrays),
        }
        _set_array(
            res,
            "work",
            [w.to_dict(include_empty_arrays=include_empty_arrays) for w in self.work],
            include_empty_arrays=include_empty_arrays,
        )
        _set_array(
            res,
            "education",
            [
                e.to_dict(include_empty_arrays=include_empty_arrays)
                for e in self.education
            ],
            include_empty_arrays=include_empty_arrays,
        )
        _set_array(
            res,
            "skills",
            [s.to_dict(include_empty_arrays=include_empty_arrays) for s in self.skills],
            include_empty_arrays=include_empty_arrays,
        )
        _set_array(
            res,
            "projects",
            [
                p.to_dict(include_empty_arrays=include_empty_arrays)
                for p in self.projects
            ],
            include_empty_arrays=include_empty_arrays,
        )
        if self.meta is not None:
            res["meta"] = self.meta
        return res
