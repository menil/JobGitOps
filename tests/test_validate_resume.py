"""Unit tests for the validate_resume CLI entry point."""

import pathlib

from jobgitops.cli import validate_resume
from jobgitops.loader import render_resume_yaml
from jobgitops.schema import Resume


def _write_canonical_resume(tmp_path: pathlib.Path) -> pathlib.Path:
    resume = Resume.from_dict(
        {
            "basics": {
                "name": "Jane Doe",
                "location": {"city": "Seattle", "region": "WA", "countryCode": "US"},
            }
        }
    )
    resume_file = tmp_path / "resume.yaml"
    resume_file.write_text(render_resume_yaml(resume), encoding="utf-8")
    return resume_file


def test_validate_nonexistent_file(tmp_path) -> None:
    path = tmp_path / "does_not_exist.yaml"
    assert validate_resume.main([str(path)]) == 1


def test_validate_invalid_yaml(tmp_path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("basics:\n  name: [invalid", encoding="utf-8")
    assert validate_resume.main([str(path)]) == 1


def test_validate_invalid_schema(tmp_path) -> None:
    path = tmp_path / "invalid_schema.yaml"
    path.write_text("basics:\n  location: 'not-a-dict'", encoding="utf-8")
    assert validate_resume.main([str(path)]) == 1


def test_validate_canonical_resume(tmp_path) -> None:
    path = _write_canonical_resume(tmp_path)
    assert validate_resume.main([str(path)]) == 0
    assert validate_resume.main([str(path), "--check-canonical"]) == 0


def test_validate_noncanonical_resume(tmp_path) -> None:
    path = _write_canonical_resume(tmp_path)
    # Add a trailing space or extra newline to make it non-canonical
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")

    # Should exit 0 on simple check because it is valid
    assert validate_resume.main([str(path)]) == 0
    # Should exit 2 on check-canonical because it's not canonical
    assert validate_resume.main([str(path), "--check-canonical"]) == 2


def test_validate_unexpected_exception(monkeypatch) -> None:
    def mock_load_resume(*args, **kwargs):
        raise RuntimeError("Unexpected error")

    monkeypatch.setattr("jobgitops.cli.validate_resume.load_resume", mock_load_resume)
    assert validate_resume.main(["dummy.yaml"]) == 1
