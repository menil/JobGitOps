"""Data schemas and dataclasses for JobGitOps."""

import datetime
from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class SearchConfig:
    """Job search scraper configuration."""

    location: str = "Remote"
    job_type: str = "fulltime"
    platforms: list[str] = field(
        default_factory=lambda: ["linkedin", "indeed", "zip_recruiter"]
    )
    hours_old: int = 24
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchConfig":
        """Parse search configuration from dictionary.

        Args:
            data: Raw dictionary containing search config fields.

        Returns:
            A parsed SearchConfig instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("Search configuration must be a dictionary.")

        try:
            location = _parse_str("search.location", data.get("location")) or "Remote"
            job_type = _parse_str("search.job_type", data.get("job_type")) or "fulltime"

            platforms_raw = data.get("platforms")
            if platforms_raw is None:
                platforms = ["linkedin", "indeed", "zip_recruiter"]
            else:
                if not isinstance(platforms_raw, list) or not all(
                    isinstance(p, str) and not isinstance(p, bool)
                    for p in platforms_raw
                ):
                    raise ValidationError("search.platforms must be a list of strings.")
                platforms = platforms_raw

            hours_old_raw = data.get("hours_old")
            if hours_old_raw is None:
                hours_old = 24
            else:
                if isinstance(hours_old_raw, bool) or not isinstance(
                    hours_old_raw, int
                ):
                    try:
                        if isinstance(hours_old_raw, bool):
                            raise TypeError()
                        hours_old = int(hours_old_raw)
                    except (ValueError, TypeError) as e:
                        raise ValidationError(
                            "search.hours_old must be an integer."
                        ) from e
                else:
                    hours_old = hours_old_raw

            if hours_old <= 0:
                raise ValidationError("search.hours_old must be greater than zero.")

            enabled_val = data.get("enabled")
            if enabled_val is None:
                enabled = True
            elif isinstance(enabled_val, str):
                enabled = enabled_val.lower() not in ("false", "0", "no", "")
            else:
                enabled = bool(enabled_val)

            return cls(
                location=location,
                job_type=job_type,
                platforms=platforms,
                hours_old=hours_old,
                enabled=enabled,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse SearchConfig: {e}") from e


@dataclass
class ProjectsV2Config:
    """Optional GitHub Projects V2 automation configuration."""

    project_id: str
    status_field_name: str = "Status"

    # Sentinel shipped in config/settings.yaml; normalized to empty below so a
    # fresh clone degrades to label-only tracking instead of firing GraphQL
    # mutations against a nonexistent project (which would red-CI every label
    # event). Enabling the integration is an explicit "replace the placeholder"
    # step.
    PLACEHOLDER_PREFIX = "PVT_YOUR_"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectsV2Config":
        """Parse Projects V2 configuration from dictionary.

        Args:
            data: Raw dictionary containing Projects V2 config.

        Returns:
            A parsed ProjectsV2Config instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("projects_v2 configuration must be a dictionary.")

        try:
            project_id = _parse_str("projects_v2.project_id", data.get("project_id"))
            if not project_id:
                raise ValidationError(
                    "projects_v2.project_id must be a non-empty string."
                )

            # A placeholder project ID is indistinguishable from "not set":
            # scripts gate Projects V2 work on project_id truthiness, so keep
            # the shipped default in a label-only state until a real ID is set.
            if project_id.startswith(cls.PLACEHOLDER_PREFIX):
                project_id = ""

            status_field_name = (
                _parse_str(
                    "projects_v2.status_field_name", data.get("status_field_name")
                )
                or cls.status_field_name
            )

            return cls(project_id=project_id, status_field_name=status_field_name)
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse ProjectsV2Config: {e}") from e


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


@dataclass
class ResearchConfig:
    """Issue Assistant research / web-tool configuration."""

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
    # Optional responder model override; empty = provider default.
    model: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchConfig":
        """Parse research configuration from dictionary.

        Args:
            data: Raw dictionary containing research config fields.

        Returns:
            A parsed ResearchConfig instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("Research configuration must be a dictionary.")

        try:
            request_delay = _parse_float(
                "research.request_delay", data.get("request_delay"), cls.request_delay
            )
            if request_delay < 0:
                raise ValidationError("research.request_delay must not be negative.")

            values: dict[str, Any] = {}
            for name in _RESEARCH_INT_FIELDS:
                values[name] = _parse_positive_int(
                    f"research.{name}", data.get(name), getattr(cls, name)
                )
            for name in _RESEARCH_BOOL_FIELDS:
                values[name] = _parse_bool(
                    f"research.{name}", data.get(name), getattr(cls, name)
                )

            return cls(
                search_provider=(
                    _parse_str("research.search_provider", data.get("search_provider"))
                    or cls.search_provider
                ),
                request_delay=request_delay,
                model=_parse_str("research.model", data.get("model")) or cls.model,
                **values,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse ResearchConfig: {e}") from e


@dataclass
class Settings:
    """App-wide settings loaded from config/settings.yaml."""

    fit_threshold: float = FIT_GRADE_B_MIN
    search: SearchConfig = field(default_factory=SearchConfig)
    custom_queries: list[str] | None = None
    projects_v2: ProjectsV2Config | None = None
    research: ResearchConfig = field(default_factory=ResearchConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Parse settings from dictionary with parsing validation.

        Args:
            data: Raw dictionary containing Settings fields.

        Returns:
            A parsed Settings instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("Settings data must be a dictionary.")

        try:
            fit_threshold_raw = data.get("fit_threshold", cls.fit_threshold)
            if isinstance(fit_threshold_raw, bool) or not isinstance(
                fit_threshold_raw, (int, float)
            ):
                try:
                    if isinstance(fit_threshold_raw, bool):
                        raise TypeError()
                    fit_threshold = float(fit_threshold_raw)
                except (ValueError, TypeError) as e:
                    raise ValidationError("fit_threshold must be a number.") from e
            else:
                fit_threshold = float(fit_threshold_raw)

            if not (1.0 <= fit_threshold <= 5.0):
                raise ValidationError("fit_threshold must be between 1.0 and 5.0.")

            search_data = data.get("search") or {}
            search = SearchConfig.from_dict(search_data)

            custom_queries_raw = data.get("custom_queries")
            if custom_queries_raw is None:
                custom_queries = None
            else:
                if not isinstance(custom_queries_raw, list) or not all(
                    isinstance(q, str) and not isinstance(q, bool)
                    for q in custom_queries_raw
                ):
                    raise ValidationError("custom_queries must be a list of strings.")
                custom_queries = custom_queries_raw

            projects_v2_data = data.get("projects_v2")
            projects_v2 = (
                ProjectsV2Config.from_dict(projects_v2_data)
                if projects_v2_data is not None
                else None
            )

            research_data = data.get("research") or {}
            research = ResearchConfig.from_dict(research_data)

            return cls(
                fit_threshold=fit_threshold,
                search=search,
                custom_queries=custom_queries,
                projects_v2=projects_v2,
                research=research,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Settings: {e}") from e


# --- JSON Resume Schema Dataclasses ---


@dataclass
class Location:
    """Location information for JSON Resume."""

    city: str | None = None
    state: str | None = None
    country_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Location":
        """Parse location details from dictionary.

        Args:
            data: Raw dictionary containing Location fields.

        Returns:
            A parsed Location instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("basics.location must be a dictionary.")

        state_val = data.get("state") or data.get("region")
        country_val = data.get("countryCode") or data.get("country_code")

        return cls(
            city=_parse_str("basics.location.city", data.get("city")),
            state=_parse_str("basics.location.state", state_val),
            country_code=_parse_str("basics.location.country_code", country_val),
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


@dataclass
class Profile:
    """Social profiles (GitHub, LinkedIn) for JSON Resume."""

    network: str
    username: str
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        """Parse social profile details from dictionary.

        Args:
            data: Raw dictionary containing Profile fields.

        Returns:
            A parsed Profile instance.

        Raises:
            ValidationError: If parsing fails.
        """
        if not isinstance(data, dict):
            raise ValidationError("profile details must be a dictionary.")

        network = _parse_str("profile.network", data.get("network"))
        if not network:
            raise ValidationError("profile.network must be a non-empty string.")

        username = _parse_str("profile.username", data.get("username"))
        if not username:
            raise ValidationError("profile.username must be a non-empty string.")

        return cls(
            network=network,
            username=username,
            url=_parse_str("profile.url", data.get("url")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Profile to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {
            "network": self.network,
            "username": self.username,
        }
        if self.url is not None:
            res["url"] = self.url
        return res


@dataclass
class Basics:
    """Basic profile information for JSON Resume."""

    name: str
    label: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
    summary: str | None = None
    location: Location | None = None
    profiles: list[Profile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Basics":
        """Parse basics section from dictionary.

        Args:
            data: Raw dictionary containing Basics fields.

        Returns:
            A parsed Basics instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

    def to_dict(self) -> dict[str, Any]:
        """Convert Basics to dictionary conforming to JSON Resume schema."""
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
            res["location"] = self.location.to_dict()
        if self.profiles:
            res["profiles"] = [p.to_dict() for p in self.profiles]
        return res


@dataclass
class Work:
    """Professional work experience entry for JSON Resume."""

    name: str
    position: str
    url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    summary: str | None = None
    highlights: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Work":
        """Parse work experience entry from dictionary.

        Args:
            data: Raw dictionary containing Work fields.

        Returns:
            A parsed Work instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

    def to_dict(self) -> dict[str, Any]:
        """Convert Work to dictionary conforming to JSON Resume schema."""
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
        if self.highlights:
            res["highlights"] = self.highlights
        return res


@dataclass
class Education:
    """Education history entry for JSON Resume."""

    institution: str
    url: str | None = None
    area: str | None = None
    study_type: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    score: str | None = None
    courses: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Education":
        """Parse education history entry from dictionary.

        Args:
            data: Raw dictionary containing Education fields.

        Returns:
            A parsed Education instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

    def to_dict(self) -> dict[str, Any]:
        """Convert Education to dictionary conforming to JSON Resume schema."""
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
        if self.courses:
            res["courses"] = self.courses
        return res


@dataclass
class Skill:
    """Professional skills entry for JSON Resume."""

    name: str
    keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        """Parse skill entry from dictionary.

        Args:
            data: Raw dictionary containing Skill fields.

        Returns:
            A parsed Skill instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

    def to_dict(self) -> dict[str, Any]:
        """Convert Skill to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {
            "name": self.name,
        }
        if self.keywords:
            res["keywords"] = self.keywords
        return res


@dataclass
class Project:
    """Personal or professional project entry for JSON Resume."""

    name: str
    description: str | None = None
    highlights: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        """Parse project entry from dictionary.

        Args:
            data: Raw dictionary containing Project fields.

        Returns:
            A parsed Project instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

    def to_dict(self) -> dict[str, Any]:
        """Convert Project to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {
            "name": self.name,
        }
        if self.description is not None:
            res["description"] = self.description
        if self.highlights:
            res["highlights"] = self.highlights
        if self.keywords:
            res["keywords"] = self.keywords
        if self.start_date is not None:
            res["startDate"] = self.start_date
        if self.end_date is not None:
            res["endDate"] = self.end_date
        if self.url is not None:
            res["url"] = self.url
        return res


@dataclass
class Resume:
    """Full resume conforming to JSON Resume schema conventions."""

    basics: Basics
    work: list[Work] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resume":
        """Parse full resume from dictionary with parsing validation.

        Args:
            data: Raw dictionary containing the full Resume.

        Returns:
            A parsed Resume instance.

        Raises:
            ValidationError: If parsing fails.
        """
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

            return cls(
                basics=basics,
                work=work,
                education=education,
                skills=skills,
                projects=projects,
            )
        except (ValueError, TypeError, ValidationError) as e:
            raise ValidationError(f"Failed to parse Resume: {e}") from e

    def to_dict(self) -> dict[str, Any]:
        """Convert Resume to dictionary conforming to JSON Resume schema."""
        res: dict[str, Any] = {
            "basics": self.basics.to_dict(),
        }
        if self.work:
            res["work"] = [w.to_dict() for w in self.work]
        if self.education:
            res["education"] = [e.to_dict() for e in self.education]
        if self.skills:
            res["skills"] = [s.to_dict() for s in self.skills]
        if self.projects:
            res["projects"] = [p.to_dict() for p in self.projects]
        return res
