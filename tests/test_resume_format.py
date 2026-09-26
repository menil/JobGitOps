"""Ensure the committed resume fixture stays in the canonical GitEmployed format."""

import pathlib

import scripts.format_resume as format_resume
from gitemployed.loader import load_resume, render_resume_yaml, resume_yaml_is_canonical
from gitemployed.schema import Resume

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


def test_canonical_omits_empty_optional_arrays_written_by_hand(tmp_path) -> None:
    """A hand-written resume lacking optional arrays is already canonical.

    Regression test: before this fix, render_resume_yaml force-populated
    empty optional arrays (highlights, courses, profiles, etc.), so any
    resume.yaml written without them -- valid per the JSON Resume schema,
    since these fields are optional -- failed the canonical-format check
    and blocked scraping/triaging until Auto-Format Resume happened to
    re-run. Confirmed against a real user resume during manual end-to-end
    testing of the theme migration.
    """
    content = (
        "basics:\n"
        "  name: Jane Doe\n"
        "  location:\n"
        "    city: Seattle\n"
        "    region: WA\n"
        "    countryCode: US\n"
        "work:\n"
        "- name: Acme\n"
        "  position: Engineer\n"
        "  startDate: '2020-01-01'\n"
    )
    resume_file = tmp_path / "resume.yaml"
    resume_file.write_text(content, encoding="utf-8")

    assert resume_yaml_is_canonical(resume_file)


def test_canonical_normalizes_explicit_empty_array_to_omitted(tmp_path) -> None:
    """An explicitly-written empty array (e.g. `highlights: []`) is not canonical.

    from_dict can't distinguish "key omitted" from "key present but empty"
    -- both collapse to the same in-memory empty list -- so canonical form
    treats them identically too: both normalize to omitted. This is the
    intended consequence of "missing optional fields aren't a formatting
    issue," not a gap: it keeps exactly one canonical spelling for "no
    highlights," rather than two (omitted and `[]`) that would both need to
    be accepted as canonical.
    """
    content = (
        "basics:\n"
        "  name: Jane Doe\n"
        "  location:\n"
        "    city: Seattle\n"
        "    region: WA\n"
        "    countryCode: US\n"
        "work:\n"
        "- name: Acme\n"
        "  position: Engineer\n"
        "  highlights: []\n"
    )
    resume_file = tmp_path / "resume.yaml"
    resume_file.write_text(content, encoding="utf-8")

    assert not resume_yaml_is_canonical(resume_file)
    assert "highlights" not in render_resume_yaml(load_resume(resume_file))


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
    """Formatting is skipped when the __GITEMPLOYED_SETUP_PENDING__ marker is
    present."""
    resume_file = tmp_path / "resume.yaml"
    content = "# __GITEMPLOYED_SETUP_PENDING__\nbasics:\n  name: Test\n"
    resume_file.write_text(content, encoding="utf-8")

    assert format_resume.main([str(resume_file)]) == 0
    assert resume_file.read_text(encoding="utf-8") == content


def test_canonical_preserves_meta_section(tmp_path: pathlib.Path) -> None:
    """Canonical serialization preserves top-level meta sections (themeOptions)."""
    content = "basics:\n  name: Jane Doe\nmeta:\n  themeOptions:\n    fitPages: auto\n"
    resume_file = tmp_path / "resume.yaml"
    resume_file.write_text(content, encoding="utf-8")

    loaded = load_resume(resume_file)
    assert loaded.meta == {"themeOptions": {"fitPages": "auto"}}
    rendered = render_resume_yaml(loaded)
    assert "meta:" in rendered
    assert "fitPages: auto" in rendered
    assert resume_yaml_is_canonical(resume_file)
