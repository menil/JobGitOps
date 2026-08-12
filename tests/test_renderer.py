"""Unit tests for the resume rendering and compilation pipeline."""

import json
import pathlib

import pytest

from jobgitops.renderer import (
    compile_resume,
    compile_resume_json,
    compile_resume_pdf,
    render_resume_to_html,
)
from jobgitops.schema import Resume


@pytest.fixture
def sample_resume_data() -> dict:
    """Provide a standard dictionary matching JSON Resume schema for testing."""
    return {
        "basics": {
            "name": "Jane Doe",
            "label": "Developer",
            "email": "jane@example.com",
            "phone": "555-1234",
            "url": "https://jane.dev",
            "summary": "Experienced coder",
            "location": {"city": "New York", "region": "NY", "countryCode": "US"},
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


def test_resume_serialization_roundtrip(sample_resume_data) -> None:
    """Verify that serialization to dict is stable and fully lossless on roundtrip."""
    # 1. Parse from standard dict structure
    resume = Resume.from_dict(sample_resume_data)

    # 2. Serialize back to dict
    serialized = resume.to_dict()

    # 3. Assert fields are mapped to standard camelCase JSON Resume naming conventions
    assert serialized["basics"]["name"] == "Jane Doe"
    assert serialized["basics"]["location"]["region"] == "NY"
    assert serialized["basics"]["location"]["countryCode"] == "US"
    assert serialized["work"][0]["startDate"] == "2020-01-01"
    assert serialized["work"][0]["endDate"] == "2023-01-01"
    assert serialized["education"][0]["studyType"] == "BS"
    assert serialized["education"][0]["startDate"] == "2016-09-01"
    assert serialized["projects"][0]["startDate"] == "2022-01-01"

    # 4. Parse the serialized structure again and assert identity equivalence
    resume_roundtrip = Resume.from_dict(serialized)
    assert resume == resume_roundtrip


def test_resume_serialization_optional_fields() -> None:
    """Verify that optional or empty fields are handled during serialization."""
    minimal_data = {
        "basics": {
            "name": "Only Name",
            "location": {"city": "Seattle", "region": "WA", "countryCode": "US"},
        }
    }
    resume = Resume.from_dict(minimal_data)
    serialized = resume.to_dict()

    assert serialized == {
        "basics": {
            "name": "Only Name",
            "location": {"city": "Seattle", "region": "WA", "countryCode": "US"},
        }
    }
    assert "work" not in serialized
    assert "education" not in serialized
    assert "skills" not in serialized
    assert "projects" not in serialized


def test_render_resume_to_html(sample_resume_data, tmp_path) -> None:
    """Verify HTML rendering successfully interpolates parsed resume details."""
    resume = Resume.from_dict(sample_resume_data)
    template_file = tmp_path / "template.html"

    # Create a simple template that uses fields from the Resume
    template_file.write_text(
        "<h1>{{ basics.name }}</h1><p>{{ basics.email }}</p>"
        "<h2>{{ work[0].position }} at {{ work[0].name }}</h2>",
        encoding="utf-8",
    )

    rendered_html = render_resume_to_html(resume, template_file)
    assert "<h1>Jane Doe</h1>" in rendered_html
    assert "<p>jane@example.com</p>" in rendered_html
    assert "<h2>Staff Engineer at Acme</h2>" in rendered_html


def test_render_resume_to_html_missing_template(sample_resume_data) -> None:
    """Verify renderer raises FileNotFoundError if template does not exist."""
    resume = Resume.from_dict(sample_resume_data)
    non_existent = pathlib.Path("does/not/exist/template.html")

    with pytest.raises(FileNotFoundError, match="Template file not found at"):
        render_resume_to_html(resume, non_existent)


def test_compile_resume_pdf(sample_resume_data, tmp_path) -> None:
    """Verify WeasyPrint successfully renders HTML to a non-empty PDF file."""
    resume = Resume.from_dict(sample_resume_data)
    template_file = tmp_path / "template.html"
    template_file.write_text(
        "<html><body><h1>{{ basics.name }}</h1></body></html>", encoding="utf-8"
    )

    output_pdf = tmp_path / "output.pdf"
    assert not output_pdf.exists()

    compile_resume_pdf(resume, template_file, output_pdf)

    assert output_pdf.is_file()
    # A valid PDF file starts with standard magic signature '%PDF'
    assert output_pdf.read_bytes().startswith(b"%PDF")


def test_compile_resume_json(sample_resume_data, tmp_path) -> None:
    """Verify resume JSON compiler generates a correct and readable JSON Resume file."""
    resume = Resume.from_dict(sample_resume_data)
    output_json = tmp_path / "resume.json"
    assert not output_json.exists()

    compile_resume_json(resume, output_json)

    assert output_json.is_file()

    with output_json.open(encoding="utf-8") as f:
        loaded_data = json.load(f)

    assert loaded_data == resume.to_dict()


def test_compile_resume_full_pipeline(sample_resume_data, tmp_path) -> None:
    """Verify compile_resume helper executes both PDF and JSON compiles successfully."""
    resume = Resume.from_dict(sample_resume_data)
    template_file = tmp_path / "template.html"
    template_file.write_text(
        "<html><body><h1>{{ basics.name }}</h1></body></html>", encoding="utf-8"
    )

    output_pdf = tmp_path / "resume.pdf"
    output_json = tmp_path / "resume.json"

    assert not output_pdf.exists()
    assert not output_json.exists()

    compile_resume(resume, template_file, output_pdf, output_json)

    assert output_pdf.is_file()
    assert output_pdf.read_bytes().startswith(b"%PDF")

    assert output_json.is_file()
    with output_json.open(encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == resume.to_dict()
