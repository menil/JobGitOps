"""Ensure the committed resume fixture stays in the canonical JobGitOps format."""

import pathlib

import scripts.format_resume as format_resume
from jobgitops.loader import render_resume_yaml, resume_yaml_is_canonical
from jobgitops.schema import Resume

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RESUME_PATH = REPO_ROOT / "tests" / "fixtures" / "resume.yaml"


def _write_canonical_resume(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write a minimal canonical resume and return its path."""
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


def test_resume_yaml_is_canonical() -> None:
    """The committed fixture resume must match its canonical serialization."""
    assert resume_yaml_is_canonical(RESUME_PATH), (
        "tests/fixtures/resume.yaml is not in canonical format; "
        "run `just format-resume` and commit the result."
    )


def test_format_check_rejects_drift(tmp_path) -> None:
    """--check exits 1 when the file drifts from canonical format."""
    resume_file = _write_canonical_resume(tmp_path)
    resume_file.write_text(
        resume_file.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )

    assert format_resume.main([str(resume_file), "--check"]) == 1


def test_format_check_accepts_canonical(tmp_path) -> None:
    """--check exits 0 when the file is already canonical."""
    resume_file = _write_canonical_resume(tmp_path)

    assert format_resume.main([str(resume_file), "--check"]) == 0


def test_format_rewrite_normalizes(tmp_path) -> None:
    """The rewrite path normalizes a drifted file back to canonical."""
    resume_file = _write_canonical_resume(tmp_path)
    resume_file.write_text(
        resume_file.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )

    assert format_resume.main([str(resume_file)]) == 0
    assert resume_yaml_is_canonical(resume_file)


def test_format_skips_if_setup_pending(tmp_path) -> None:
    """Formatting is skipped when the __JOBGITOPS_SETUP_PENDING__ marker is present."""
    resume_file = tmp_path / "resume.yaml"
    content = "# __JOBGITOPS_SETUP_PENDING__\nbasics:\n  name: Test\n"
    resume_file.write_text(content, encoding="utf-8")

    assert format_resume.main([str(resume_file)]) == 0
    assert resume_file.read_text(encoding="utf-8") == content
