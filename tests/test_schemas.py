"""Tests for JobGitOps data schemas and loading utilities."""

import pathlib
from unittest.mock import patch

import pytest

from jobgitops.loader import load_resume, load_settings
from jobgitops.schema import Basics, ResearchConfig, Resume, Settings, ValidationError


def test_default_settings() -> None:
    """Test that Settings returns proper defaults when created or empty."""
    settings = Settings()
    assert settings.fit_threshold == 3.5
    assert settings.search.location == "Remote"
    assert settings.search.job_type == "fulltime"
    assert "linkedin" in settings.search.platforms
    assert settings.custom_queries is None
    assert settings.projects_v2 is None
    assert settings.research.search_provider == "duckduckgo"
    assert settings.research.max_results == 5
    assert settings.research.max_iterations == 6
    assert settings.research.max_context_comments == 10
    assert settings.research.timeout_seconds == 15
    assert settings.research.total_timeout_seconds == 30
    assert settings.research.max_redirects == 5
    assert settings.research.max_content_bytes == 1048576
    assert settings.research.request_delay == 1.0
    assert settings.research.use_jina_reader is True
    assert settings.research.max_jina_calls == 5
    assert settings.research.block_private_ips is True
    assert settings.research.model == ""


def test_valid_settings_parsing() -> None:
    """Test parsing a valid settings dictionary."""
    data = {
        "fit_threshold": 3.5,
        "search": {
            "location": "Seattle, WA",
            "job_type": "contract",
            "platforms": ["indeed"],
            "hours_old": 48,
        },
        "custom_queries": ["Python Developer"],
        "projects_v2": {"project_id": "PVT_123", "status_field_name": "Job Status"},
        "research": {
            "search_provider": "tavily",
            "max_results": 3,
            "max_iterations": 4,
            "max_context_comments": 8,
            "timeout_seconds": 10,
            "total_timeout_seconds": 20,
            "max_redirects": 3,
            "max_content_bytes": 1024,
            "request_delay": 0.5,
            "use_jina_reader": False,
            "max_jina_calls": 2,
            "block_private_ips": False,
            "model": "models/gemini-2.5-flash",
        },
    }
    settings = Settings.from_dict(data)
    assert settings.fit_threshold == 3.5
    assert settings.search.location == "Seattle, WA"
    assert settings.search.job_type == "contract"
    assert settings.search.platforms == ["indeed"]
    assert settings.search.hours_old == 48
    assert settings.custom_queries == ["Python Developer"]
    assert settings.projects_v2 is not None
    assert settings.projects_v2.project_id == "PVT_123"
    assert settings.projects_v2.status_field_name == "Job Status"
    assert settings.research.search_provider == "tavily"
    assert settings.research.max_results == 3
    assert settings.research.max_iterations == 4
    assert settings.research.max_context_comments == 8
    assert settings.research.timeout_seconds == 10
    assert settings.research.total_timeout_seconds == 20
    assert settings.research.max_redirects == 3
    assert settings.research.max_content_bytes == 1024
    assert settings.research.request_delay == 0.5
    assert settings.research.use_jina_reader is False
    assert settings.research.max_jina_calls == 2
    assert settings.research.block_private_ips is False
    assert settings.research.model == "models/gemini-2.5-flash"


def test_invalid_settings_types() -> None:
    """Test validation errors for invalid setting types, bounds, and booleans."""
    with pytest.raises(ValidationError, match="Settings data must be a dictionary"):
        Settings.from_dict("not-a-dict")  # type: ignore

    with pytest.raises(ValidationError, match="fit_threshold must be a number"):
        Settings.from_dict({"fit_threshold": "not-a-number"})

    # Test boolean is not accepted as a number for fit_threshold
    with pytest.raises(ValidationError, match="fit_threshold must be a number"):
        Settings.from_dict({"fit_threshold": True})

    # Test bounds checking for fit_threshold
    with pytest.raises(
        ValidationError, match="fit_threshold must be between 1.0 and 5.0"
    ):
        Settings.from_dict({"fit_threshold": 0.5})

    with pytest.raises(
        ValidationError, match="fit_threshold must be between 1.0 and 5.0"
    ):
        Settings.from_dict({"fit_threshold": 5.1})

    with pytest.raises(ValidationError, match="search.location must be a string"):
        Settings.from_dict({"search": {"location": []}})

    with pytest.raises(ValidationError, match="search.job_type must be a string"):
        Settings.from_dict({"search": {"job_type": True}})

    with pytest.raises(ValidationError, match="search.platforms must be a list"):
        Settings.from_dict({"search": {"platforms": "linkedin"}})

    # Test boolean is not accepted in platforms list
    with pytest.raises(
        ValidationError, match="search.platforms must be a list of strings"
    ):
        Settings.from_dict({"search": {"platforms": ["linkedin", True]}})

    # Test non-string is not accepted in platforms list
    with pytest.raises(
        ValidationError, match="search.platforms must be a list of strings"
    ):
        Settings.from_dict({"search": {"platforms": ["linkedin", 123]}})

    with pytest.raises(ValidationError, match="search.hours_old must be an integer"):
        Settings.from_dict({"search": {"hours_old": "not-an-int"}})

    # Test boolean is not accepted as an integer for hours_old
    with pytest.raises(ValidationError, match="search.hours_old must be an integer"):
        Settings.from_dict({"search": {"hours_old": True}})

    # Test bounds checking for hours_old
    with pytest.raises(
        ValidationError, match="search.hours_old must be greater than zero"
    ):
        Settings.from_dict({"search": {"hours_old": 0}})

    with pytest.raises(
        ValidationError, match="search.hours_old must be greater than zero"
    ):
        Settings.from_dict({"search": {"hours_old": -5}})

    with pytest.raises(ValidationError, match="custom_queries must be a list"):
        Settings.from_dict({"custom_queries": "query"})

    with pytest.raises(
        ValidationError, match="custom_queries must be a list of strings"
    ):
        Settings.from_dict({"custom_queries": ["query", True]})

    msg = "projects_v2.project_id must be a non-empty string"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"projects_v2": {"project_id": ""}})

    msg = "projects_v2.status_field_name must be a string"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict(
            {"projects_v2": {"project_id": "PVT_123", "status_field_name": []}}
        )


def test_projects_v2_placeholder_id_is_treated_as_unset() -> None:
    """Verify the shipped PVT_YOUR_ placeholder disables the integration.

    A placeholder (e.g. PVT_YOUR_PROJECT_ID) must behave exactly like an empty
    project_id so scripts fall back to label-only tracking instead of firing
    GraphQL mutations against a nonexistent board.
    """
    settings = Settings.from_dict(
        {
            "projects_v2": {
                "project_id": "PVT_YOUR_PROJECT_ID",
                "status_field_name": "Status",
            }
        }
    )
    assert settings.projects_v2 is not None
    assert settings.projects_v2.project_id == ""


def test_invalid_research_types() -> None:
    """Test validation errors for invalid research setting types and bounds."""
    msg = "research.search_provider must be a string, not a boolean"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"search_provider": True}})

    msg = "research.model must be a string, not a boolean"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"model": False}})

    msg = "research.max_results must be an integer"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_results": "not-an-int"}})

    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_results": True}})

    msg = "research.max_results must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_results": 0}})

    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_results": -5}})

    msg = "research.max_iterations must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_iterations": 0}})

    msg = "research.max_context_comments must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_context_comments": 0}})

    msg = "research.timeout_seconds must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"timeout_seconds": 0}})

    msg = "research.total_timeout_seconds must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"total_timeout_seconds": 0}})

    msg = "research.max_redirects must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_redirects": 0}})

    msg = "research.max_content_bytes must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_content_bytes": 0}})

    msg = "research.request_delay must be a number"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"request_delay": "slow"}})

    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"request_delay": True}})

    msg = "research.request_delay must not be negative"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"request_delay": -1}})

    msg = "research.use_jina_reader must be a boolean"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"use_jina_reader": "yes"}})

    msg = "research.max_jina_calls must be greater than zero"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"max_jina_calls": 0}})

    msg = "research.block_private_ips must be a boolean"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": {"block_private_ips": "yes"}})

    msg = "Research configuration must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"research": "not-a-dict"})  # type: ignore


def test_research_section_defaults_when_empty() -> None:
    """Test that a null or empty research section parses to defaults."""
    for research in (None, {}):
        settings = Settings.from_dict({"research": research})  # type: ignore
        assert settings.research.search_provider == "duckduckgo"
        assert settings.research.max_results == 5
        assert settings.research.max_iterations == 6
        assert settings.research.max_context_comments == 10
        assert settings.research.timeout_seconds == 15
        assert settings.research.total_timeout_seconds == 30
        assert settings.research.max_redirects == 5
        assert settings.research.max_content_bytes == 1048576
        assert settings.research.request_delay == 1.0
        assert settings.research.use_jina_reader is True
        assert settings.research.max_jina_calls == 5
        assert settings.research.block_private_ips is True
        assert settings.research.model == ""


def test_valid_resume_parsing() -> None:
    """Test parsing a valid resume dictionary and assert all data attributes."""
    data = {
        "basics": {
            "name": "Jane Doe",
            "label": "Developer",
            "email": "jane@example.com",
            "phone": "555-1234",
            "url": "https://jane.dev",
            "summary": "Experienced coder",
            "location": {"city": "New York", "state": "NY", "countryCode": "US"},
            "profiles": [
                {
                    "network": "GitHub",
                    "username": "janedoe",
                    "url": "https://github.com/janedoe",
                }
            ],
        },
        "work": [
            {
                "name": "Acme",
                "position": "Staff Engineer",
                "url": "https://acme.example",
                "startDate": "2020-01-01",
                "endDate": "2023-01-01",
                "summary": "Wrote code",
                "highlights": ["Refactored backend", "Mentored team"],
            }
        ],
        "education": [
            {
                "institution": "MIT",
                "url": "https://mit.edu",
                "area": "EECS",
                "studyType": "BS",
                "startDate": "2016-09-01",
                "endDate": "2020-06-01",
                "score": "4.0",
                "courses": ["Intro to CS"],
            }
        ],
        "skills": [{"name": "Languages", "keywords": ["Python", "Rust"]}],
        "projects": [
            {
                "name": "Project X",
                "description": "Secret project",
                "highlights": ["Delivered early"],
                "keywords": ["Python"],
                "startDate": "2022-01-01",
                "endDate": "2022-06-01",
                "url": "https://projectx.example",
            }
        ],
    }
    resume = Resume.from_dict(data)

    # Assert Basics
    assert resume.basics.name == "Jane Doe"
    assert resume.basics.label == "Developer"
    assert resume.basics.email == "jane@example.com"
    assert resume.basics.phone == "555-1234"
    assert resume.basics.url == "https://jane.dev"
    assert resume.basics.summary == "Experienced coder"

    # Assert Location
    assert resume.basics.location is not None
    assert resume.basics.location.city == "New York"
    assert resume.basics.location.state == "NY"
    assert resume.basics.location.country_code == "US"

    # Assert Profile
    assert len(resume.basics.profiles) == 1
    assert resume.basics.profiles[0].network == "GitHub"
    assert resume.basics.profiles[0].username == "janedoe"
    assert resume.basics.profiles[0].url == "https://github.com/janedoe"

    # Assert Work
    assert len(resume.work) == 1
    assert resume.work[0].name == "Acme"
    assert resume.work[0].position == "Staff Engineer"
    assert resume.work[0].url == "https://acme.example"
    assert resume.work[0].start_date == "2020-01-01"
    assert resume.work[0].end_date == "2023-01-01"
    assert resume.work[0].summary == "Wrote code"
    assert resume.work[0].highlights == ["Refactored backend", "Mentored team"]

    # Assert Education
    assert len(resume.education) == 1
    assert resume.education[0].institution == "MIT"
    assert resume.education[0].url == "https://mit.edu"
    assert resume.education[0].area == "EECS"
    assert resume.education[0].study_type == "BS"
    assert resume.education[0].start_date == "2016-09-01"
    assert resume.education[0].end_date == "2020-06-01"
    assert resume.education[0].score == "4.0"
    assert resume.education[0].courses == ["Intro to CS"]

    # Assert Skills
    assert len(resume.skills) == 1
    assert resume.skills[0].name == "Languages"
    assert resume.skills[0].keywords == ["Python", "Rust"]

    # Assert Projects
    assert len(resume.projects) == 1
    assert resume.projects[0].name == "Project X"
    assert resume.projects[0].description == "Secret project"
    assert resume.projects[0].highlights == ["Delivered early"]
    assert resume.projects[0].keywords == ["Python"]
    assert resume.projects[0].start_date == "2022-01-01"
    assert resume.projects[0].end_date == "2022-06-01"
    assert resume.projects[0].url == "https://projectx.example"


def test_invalid_resume_parsing() -> None:
    """Test validation errors for invalid resume structure and item type checks."""
    with pytest.raises(ValidationError, match="Resume data must be a dictionary"):
        Resume.from_dict([])  # type: ignore

    with pytest.raises(ValidationError, match="basics section is required"):
        Resume.from_dict({"work": []})

    with pytest.raises(ValidationError, match="basics.name is required"):
        Resume.from_dict({"basics": {}})

    msg = "basics.profiles must be a list"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict({"basics": {"name": "Jane", "profiles": "not-a-list"}})

    # Profile missing username
    msg = "profile.username must be a non-empty string"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane", "profiles": [{"network": "GitHub"}]}}
        )

    with pytest.raises(ValidationError, match="work section must be a list"):
        Resume.from_dict({"basics": {"name": "Jane"}, "work": "not-a-list"})

    with pytest.raises(ValidationError, match="work.name is required"):
        Resume.from_dict({"basics": {"name": "Jane"}, "work": [{"position": "Eng"}]})

    with pytest.raises(ValidationError, match="work.position is required"):
        Resume.from_dict({"basics": {"name": "Jane"}, "work": [{"name": "Acme"}]})

    with pytest.raises(ValidationError, match="work.highlights must be a list"):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "work": [
                    {
                        "name": "Acme",
                        "position": "Eng",
                        "highlights": "high",
                    }
                ],
            }
        )

    # Work highlights list containing non-string
    with pytest.raises(
        ValidationError, match="work.highlights must be a list of strings"
    ):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "work": [
                    {
                        "name": "Acme",
                        "position": "Eng",
                        "highlights": ["Refactored", True],
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="education section must be a list"):
        Resume.from_dict({"basics": {"name": "Jane"}, "education": "not-a-list"})

    with pytest.raises(ValidationError, match="education.institution is required"):
        Resume.from_dict({"basics": {"name": "Jane"}, "education": [{"area": "CS"}]})

    with pytest.raises(ValidationError, match="education.courses must be a list"):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "education": [
                    {
                        "institution": "MIT",
                        "courses": "none",
                    }
                ],
            }
        )

    # Education courses list containing non-string
    with pytest.raises(
        ValidationError, match="education.courses must be a list of strings"
    ):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "education": [
                    {
                        "institution": "MIT",
                        "courses": ["Intro", 123],
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="skills section must be a list"):
        Resume.from_dict({"basics": {"name": "Jane"}, "skills": "not-a-list"})

    with pytest.raises(ValidationError, match="skill.name is required"):
        Resume.from_dict({"basics": {"name": "Jane"}, "skills": [{"keywords": []}]})

    with pytest.raises(ValidationError, match="skill.keywords must be a list"):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "skills": [
                    {
                        "name": "Languages",
                        "keywords": "Python",
                    }
                ],
            }
        )

    # Skill keywords list containing non-string
    with pytest.raises(
        ValidationError, match="skill.keywords must be a list of strings"
    ):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "skills": [
                    {
                        "name": "Languages",
                        "keywords": ["Python", False],
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="projects section must be a list"):
        Resume.from_dict({"basics": {"name": "Jane"}, "projects": "not-a-list"})

    with pytest.raises(ValidationError, match="project.name is required"):
        Resume.from_dict(
            {"basics": {"name": "Jane"}, "projects": [{"description": "desc"}]}
        )

    with pytest.raises(ValidationError, match="project.highlights must be a list"):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "projects": [
                    {
                        "name": "P1",
                        "highlights": "high",
                    }
                ],
            }
        )

    # Project keywords must be a list
    with pytest.raises(
        ValidationError, match="project.keywords must be a list of strings"
    ):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "projects": [
                    {
                        "name": "P1",
                        "keywords": "Python",
                    }
                ],
            }
        )

    # Project highlights list containing non-string
    with pytest.raises(
        ValidationError, match="project.highlights must be a list of strings"
    ):
        Resume.from_dict(
            {
                "basics": {"name": "Jane"},
                "projects": [
                    {
                        "name": "P1",
                        "highlights": [True],
                    }
                ],
            }
        )


def test_load_settings(tmp_path: pathlib.Path) -> None:
    """Test loading settings from a temporary file."""
    # Test file doesn't exist
    non_existent = tmp_path / "non_existent.yaml"
    settings = load_settings(non_existent)
    assert settings.fit_threshold == 3.5

    # Test valid yaml file
    valid_file = tmp_path / "settings.yaml"
    content = "fit_threshold: 4.5\nsearch:\n  location: 'Hybrid'"
    valid_file.write_text(content, encoding="utf-8")
    settings = load_settings(valid_file)
    assert settings.fit_threshold == 4.5
    assert settings.search.location == "Hybrid"

    # Test empty yaml file
    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("", encoding="utf-8")
    settings = load_settings(empty_file)
    assert settings.fit_threshold == 3.5

    # Test malformed yaml file
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("fit_threshold: [unclosed list", encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid YAML syntax"):
        load_settings(bad_file)


def test_load_resume(tmp_path: pathlib.Path) -> None:
    """Test loading resume from a temporary file."""
    # Test file doesn't exist
    non_existent = tmp_path / "non_existent.yaml"
    with pytest.raises(FileNotFoundError):
        load_resume(non_existent)

    # Test valid resume file
    valid_file = tmp_path / "resume.yaml"
    valid_file.write_text("basics:\n  name: 'Jane Doe'", encoding="utf-8")
    resume = load_resume(valid_file)
    assert resume.basics.name == "Jane Doe"

    # Test malformed yaml file
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("basics: [unclosed", encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid YAML syntax"):
        load_resume(bad_file)

    # Test non-dictionary top-level
    not_dict_file = tmp_path / "list.yaml"
    not_dict_file.write_text("- item 1\n- item 2", encoding="utf-8")
    msg = "Resume YAML content must represent a dictionary"
    with pytest.raises(ValidationError, match=msg):
        load_resume(not_dict_file)


def test_more_validation_errors() -> None:
    """Test various secondary schema validations to achieve 100% coverage."""
    # Location not a dict
    msg = "basics.location must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict({"basics": {"name": "Jane", "location": "Seattle"}})

    # Profile not a dict
    msg = "profile details must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane", "profiles": ["not-a-dict"]}}  # type: ignore
        )

    # Work not a dict
    msg = "work entry must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane"}, "work": ["not-a-dict"]}  # type: ignore
        )

    # Education not a dict
    msg = "education entry must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane"}, "education": ["not-a-dict"]}  # type: ignore
        )

    # Skill not a dict
    msg = "skill entry must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane"}, "skills": ["not-a-dict"]}  # type: ignore
        )

    # Project not a dict
    msg = "project entry must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict(
            {"basics": {"name": "Jane"}, "projects": ["not-a-dict"]}  # type: ignore
        )

    # Basics not a dict (Resume level validation)
    msg = "basics section must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Resume.from_dict({"basics": "not-a-dict"})  # type: ignore

    # Resume basics validation - basics data not a dict passed directly
    with pytest.raises(ValidationError, match="Resume basics must be a dictionary"):
        Basics.from_dict("not-a-dict")  # type: ignore

    # ProjectsV2Config not a dict
    msg = "projects_v2 configuration must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"projects_v2": "not-a-dict"})  # type: ignore

    # SearchConfig not a dict
    msg = "Search configuration must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        Settings.from_dict({"search": "not-a-dict"})  # type: ignore

    # ResearchConfig not a dict
    msg = "Research configuration must be a dictionary"
    with pytest.raises(ValidationError, match=msg):
        ResearchConfig.from_dict("not-a-dict")  # type: ignore

    # ResearchConfig nested wrapper surfaces the offending field
    msg = "Failed to parse ResearchConfig"
    with pytest.raises(ValidationError, match=msg):
        ResearchConfig.from_dict({"max_redirects": True})


def test_loader_exceptions(tmp_path: pathlib.Path) -> None:
    """Test loader exceptions when files cannot be read."""
    # Test read settings exception
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("fit_threshold: 4.0", encoding="utf-8")
    with (
        patch("pathlib.Path.open", side_effect=OSError("Read error")),
        pytest.raises(ValidationError, match="Failed to read settings file"),
    ):
        load_settings(settings_file)

    # Test read resume exception
    resume_file = tmp_path / "resume.yaml"
    resume_file.write_text("basics:\n  name: 'Jane'", encoding="utf-8")
    with (
        patch("pathlib.Path.open", side_effect=OSError("Read error")),
        pytest.raises(ValidationError, match="Failed to read resume file"),
    ):
        load_resume(resume_file)


def test_repo_defaults_integration() -> None:
    """Test actual committed repository config and resume files."""
    # Load and assert repository default settings
    settings = load_settings("config/settings.yaml")
    assert settings.fit_threshold == 3.5
    assert settings.search.location == "Remote"
    assert settings.search.job_type == "fulltime"
    assert "linkedin" in settings.search.platforms
    assert settings.research.search_provider == "duckduckgo"
    assert settings.research.max_results == 5
    assert settings.research.max_iterations == 6
    assert settings.research.max_context_comments == 10
    assert settings.research.timeout_seconds == 15
    assert settings.research.total_timeout_seconds == 30
    assert settings.research.max_redirects == 5
    assert settings.research.max_content_bytes == 1048576
    assert settings.research.request_delay == 1.0
    assert settings.research.use_jina_reader is True
    assert settings.research.max_jina_calls == 5
    assert settings.research.block_private_ips is True
    assert settings.research.model == ""

    # Load and assert repository default resume
    resume = load_resume("resumes/resume.yaml")
    assert resume.basics.name == "Martin Birkenbaum"
    assert resume.basics.email == "martin.birkenbaum@initech.example.com"
    assert resume.basics.location is not None
    assert resume.basics.location.city == "Seattle"
    assert resume.basics.location.state == "WA"
    assert resume.basics.location.country_code == "US"
    assert len(resume.basics.profiles) == 1
    assert resume.basics.profiles[0].network == "GitHub"
    assert resume.basics.profiles[0].username == "mbirkenbaum"

    # Assert work experience loaded cleanly
    assert len(resume.work) == 2
    assert resume.work[0].name == "Initech Corporation"
    assert resume.work[1].name == "Hooli Inc."


def test_region_fallback_location() -> None:
    """Test Location parses 'region' as state for standard JSON Resume compatibility."""
    data = {
        "city": "Paris",
        "region": "Île-de-France",
        "countryCode": "FR",
    }
    loc = Resume.from_dict(
        {"basics": {"name": "Jean", "location": data}}
    ).basics.location
    assert loc is not None
    assert loc.state == "Île-de-France"
    assert loc.country_code == "FR"


def test_scalar_coercion() -> None:
    """Test unquoted floats and date objects are coerced to strings safely."""
    import datetime

    data = {
        "basics": {
            "name": "Coerced Doe",
            "phone": 1234567890,  # parsed as int
            "url": "https://example.com",
        },
        "education": [
            {
                "institution": "University",
                "score": 3.8,  # parsed as float
                "startDate": datetime.date(2020, 1, 1),  # parsed as date object
                "endDate": datetime.date(2024, 6, 1),
            }
        ],
    }
    resume = Resume.from_dict(data)
    assert resume.basics.phone == "1234567890"
    assert resume.education[0].score == "3.8"
    assert resume.education[0].start_date == "2020-01-01"
    assert resume.education[0].end_date == "2024-06-01"


def test_boolean_string_validation_raises() -> None:
    """Test that boolean values are rejected for string scalar fields."""
    data = {
        "basics": {
            "name": "Jane",
            "phone": True,  # should raise ValidationError, not coerce to "True"
        }
    }
    with pytest.raises(
        ValidationError, match="basics.phone must be a string, not a boolean"
    ):
        Resume.from_dict(data)
