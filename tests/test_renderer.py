"""Unit tests for the resume rendering and compilation pipeline."""

import json
import subprocess
from unittest import mock

import pytest

from jobgitops.renderer import (
    _ALLOWED_ENV_VARS,
    _BUN_BIN,
    ThemeInstallError,
    _parse_installed_theme_name,
    _theme_bare_name,
    _theme_looks_pinned,
    compile_resume,
    compile_resume_json,
    compile_resume_pdf,
    ensure_theme_installed,
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


def _fake_completed_process(returncode: int = 0, stderr: str = "", stdout: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


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
    """Verify array-typed JSON Resume sections are always present, even when empty.

    Real themes (e.g. @jsonresume/jsonresume-theme-professional) assume
    fields like basics.profiles or the top-level work/education/skills/
    projects arrays exist and crash on a bare `.find()`/`.map()` call when
    the key is omitted entirely -- confirmed by hands-on testing while
    building JobGitOps-184.
    """
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
            "profiles": [],
        },
        "work": [],
        "education": [],
        "skills": [],
        "projects": [],
    }


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


@pytest.mark.parametrize(
    ("theme_spec", "expected"),
    [
        ("@jsonresume/jsonresume-theme-professional@1.0.22", True),
        ("jsonresume-theme-elegant@1.16.1", True),
        ("lodash@4.0.0", True),
        ("@jsonresume/jsonresume-theme-professional", False),  # no version at all
        ("lodash", False),  # no version at all
        (f"github:owner/repo#{'a' * 40}", True),  # full 40-char commit sha
        ("github:owner/repo#main", False),  # branch, not a sha
        ("github:owner/repo#abc123", False),  # short/abbreviated sha
        ("github:owner/repo", False),  # no ref at all
    ],
)
def test_theme_looks_pinned(theme_spec: str, expected: bool) -> None:
    """Verify the pin-format check matches settings.yaml's documented rules."""
    assert _theme_looks_pinned(theme_spec) is expected


@pytest.mark.parametrize(
    ("theme_spec", "expected_bare_name"),
    [
        (
            "@jsonresume/jsonresume-theme-professional@1.0.22",
            "@jsonresume/jsonresume-theme-professional",
        ),
        ("jsonresume-theme-elegant@1.16.1", "jsonresume-theme-elegant"),
        ("lodash@4.0.0", "lodash"),
        # No version suffix at all -- must not be truncated to "" by
        # mistaking the scope-marker "@" for a version separator.
        (
            "@jsonresume/jsonresume-theme-professional",
            "@jsonresume/jsonresume-theme-professional",
        ),
        ("lodash", "lodash"),
    ],
)
def test_theme_bare_name_npm_specs(theme_spec: str, expected_bare_name: str) -> None:
    """Verify bare-name extraction for npm-style specs, including edge cases."""
    assert _theme_bare_name(theme_spec) == expected_bare_name


def test_theme_bare_name_github_spec_is_unknown_until_installed() -> None:
    """Verify a github: spec's bare name can't be derived from the spec alone."""
    assert _theme_bare_name("github:owner/repo#" + "a" * 40) is None


@pytest.mark.parametrize(
    ("bun_stdout", "expected_name"),
    [
        (
            "installed jsonresume-theme-stackoverflow@3.3.0\n\n1 package installed",
            "jsonresume-theme-stackoverflow",
        ),
        ("installed resumed@7.0.0 with binaries:\n - resumed\n", "resumed"),
        (
            "installed jsonresume-theme-stackoverflow"
            "@github:phoinixi/jsonresume-theme-stackoverflow#868d6db",
            "jsonresume-theme-stackoverflow",
        ),
        (
            "installed @jsonresume/jsonresume-theme-professional@1.0.22",
            "@jsonresume/jsonresume-theme-professional",
        ),
    ],
)
def test_parse_installed_theme_name(bun_stdout: str, expected_name: str) -> None:
    """Verify bun's own install confirmation line is parsed correctly.

    Exact formats (including the github-spec case) confirmed by hands-on
    testing against real `bun add -g` output while building this task.
    """
    assert _parse_installed_theme_name(bun_stdout, "irrelevant-spec") == expected_name


def test_parse_installed_theme_name_raises_if_no_installed_line() -> None:
    """Verify a clear error when bun's output doesn't contain the expected line."""
    with pytest.raises(ThemeInstallError, match="Could not determine"):
        _parse_installed_theme_name("Resolving dependencies\n", "some-spec")


def test_ensure_theme_installed_skips_install_when_already_present() -> None:
    """Verify no install subprocess runs for an already-installed npm theme.

    The interface check still runs even on this fast path (see
    test_ensure_theme_installed_still_validates_already_installed_theme) --
    this test only asserts that `bun add -g` itself is skipped.
    """
    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=True),
        mock.patch(
            "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
        ) as mock_run,
    ):
        result = ensure_theme_installed(
            "@jsonresume/jsonresume-theme-professional@1.0.22"
        )

    assert result == "@jsonresume/jsonresume-theme-professional"
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert argv[:2] == [_BUN_BIN, "-e"]  # the interface check, not install


def test_ensure_theme_installed_still_validates_already_installed_theme() -> None:
    """Verify a bad already-installed theme is still caught, not skipped.

    A directory merely existing (e.g. a corrupted image layer, a partial
    install left over from a previous run) is not proof the package is
    usable -- confirmed as a real gap during review, since the fast path
    used to return before validation ever ran.
    """
    validate_failure = _fake_completed_process(returncode=1)
    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=True),
        mock.patch("jobgitops.renderer.subprocess.run", return_value=validate_failure),
        pytest.raises(ThemeInstallError, match="does not export a usable"),
    ):
        ensure_theme_installed("@jsonresume/jsonresume-theme-professional@1.0.22")


def test_ensure_theme_installed_installs_npm_spec_when_missing() -> None:
    """Verify a missing npm theme is installed, then interface-validated."""
    install_result = _fake_completed_process(
        stdout="installed some-theme@2.0.0\n\n1 package installed"
    )
    validate_result = _fake_completed_process()

    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=False),
        mock.patch(
            "jobgitops.renderer.subprocess.run",
            side_effect=[install_result, validate_result],
        ) as mock_run,
    ):
        result = ensure_theme_installed("some-theme@2.0.0")

    assert result == "some-theme"
    assert mock_run.call_count == 2
    install_argv = mock_run.call_args_list[0].args[0]
    assert install_argv == [_BUN_BIN, "add", "-g", "some-theme@2.0.0"]


def test_ensure_theme_installed_uses_filtered_env_for_install_and_validate(
    monkeypatch,
) -> None:
    """Verify install/validate subprocesses don't inherit unfiltered secrets.

    _validate_theme_interface actually import()s the theme package's own
    module-level code -- an unfiltered environment there would hand every
    secret in the process to that third-party code, running as root, before
    privilege is ever dropped -- confirmed as a real gap during review.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    install_result = _fake_completed_process(stdout="installed some-theme@2.0.0")
    validate_result = _fake_completed_process()

    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=False),
        mock.patch(
            "jobgitops.renderer.subprocess.run",
            side_effect=[install_result, validate_result],
        ) as mock_run,
    ):
        ensure_theme_installed("some-theme@2.0.0")

    for call in mock_run.call_args_list:
        assert "GITHUB_TOKEN" not in call.kwargs["env"]


def test_ensure_theme_installed_resolves_github_spec_name_from_output() -> None:
    """Verify a github: spec's real name is discovered from bun's own report."""
    theme_spec = f"github:owner/some-theme#{'a' * 40}"
    install_result = _fake_completed_process(
        stdout=f"installed some-theme@{theme_spec.removeprefix('github:')}"
    )
    validate_result = _fake_completed_process()

    with mock.patch(
        "jobgitops.renderer.subprocess.run",
        side_effect=[install_result, validate_result],
    ):
        # No _theme_is_installed mock needed: a github spec's bare name is
        # unknown up front, so ensure_theme_installed can't check presence
        # before installing -- it always attempts the install for these.
        result = ensure_theme_installed(theme_spec)

    assert result == "some-theme"


def test_ensure_theme_installed_raises_on_install_failure(caplog) -> None:
    """Verify a failed bun add -g raises without leaking raw output.

    The full stderr is logged (for operators reading Actions run logs), but
    the exception message itself stays generic, since callers (triage.py)
    post exception messages verbatim as GitHub issue comments -- mirrors
    compile_resume_pdf's identical error-handling pattern.
    """
    failure = _fake_completed_process(
        returncode=1, stderr="GET https://registry.npmjs.org/some-theme - 404"
    )
    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=False),
        mock.patch("jobgitops.renderer.subprocess.run", return_value=failure),
        pytest.raises(ThemeInstallError) as exc_info,
    ):
        ensure_theme_installed("some-theme@9.9.9")

    assert "404" not in str(exc_info.value)
    assert "workflow run logs" in str(exc_info.value)
    assert "404" in caplog.text


def test_ensure_theme_installed_raises_on_non_conforming_package() -> None:
    """Verify an installed package without a render() export is rejected."""
    install_result = _fake_completed_process(stdout="installed left-pad@1.3.0")
    validate_failure = _fake_completed_process(returncode=1)

    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=False),
        mock.patch(
            "jobgitops.renderer.subprocess.run",
            side_effect=[install_result, validate_failure],
        ),
        pytest.raises(ThemeInstallError, match="does not export a usable"),
    ):
        ensure_theme_installed("left-pad@1.3.0")


def test_ensure_theme_installed_warns_on_unpinned_spec(caplog) -> None:
    """Verify an unpinned theme spec logs a warning before installing."""
    with (
        mock.patch("jobgitops.renderer._theme_is_installed", return_value=True),
        mock.patch(
            "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
        ),
    ):
        ensure_theme_installed("@jsonresume/jsonresume-theme-professional")

    assert "not pinned" in caplog.text


def test_compile_resume_pdf_invokes_resumed_as_pptruser(
    sample_resume_data, tmp_path
) -> None:
    """Verify compile_resume_pdf shells out to resumed via the validated invocation.

    Must be `su -s /bin/sh pptruser -c "bun /opt/bun/bin/resumed export ..."`,
    NOT `bunx resumed` (bunx re-resolves into an isolated cache and can't see
    the globally-installed theme) and NOT as root (Chromium runs unprivileged
    even though --no-sandbox is still required) -- see the Dockerfile.
    """
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)
    output_pdf = tmp_path / "resume.pdf"

    with mock.patch(
        "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
    ) as mock_run:
        compile_resume_pdf(
            resume_json, "@jsonresume/jsonresume-theme-professional", output_pdf
        )

    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert argv[:5] == ["su", "-s", "/bin/sh", "pptruser", "-c"]

    inner_command = argv[5]
    assert _BUN_BIN in inner_command
    assert "/opt/bun/bin/resumed" in inner_command
    assert "export" in inner_command
    assert str(resume_json.resolve()) in inner_command
    assert "@jsonresume/jsonresume-theme-professional" in inner_command
    assert str(output_pdf.resolve()) in inner_command
    assert "--puppeteer-arg=--no-sandbox" in inner_command


def test_compile_resume_pdf_chgrps_and_narrows_output_dir_to_render_user_group(
    sample_resume_data, tmp_path
) -> None:
    """Verify the output directory is handed to _RENDER_USER's group (0o775).

    Preferred over a blanket world-writable 0o777: the directory is typically
    created by the caller (e.g. triage.py's git checkout handling) as root,
    not `pptruser`, which would otherwise get EACCES writing the PDF into a
    directory it doesn't own -- confirmed as a real bug during review, not
    just a theoretical one.
    """
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)

    output_dir = tmp_path / "resumes"
    output_dir.mkdir(mode=0o700)  # simulate a restrictive, non-pptruser-owned dir
    output_pdf = output_dir / "resume.pdf"

    fake_group = mock.MagicMock(gr_gid=4242)
    with (
        mock.patch(
            "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
        ),
        mock.patch("jobgitops.renderer.grp.getgrnam", return_value=fake_group),
        mock.patch("jobgitops.renderer.os.chown") as mock_chown,
    ):
        compile_resume_pdf(resume_json, "some-theme", output_pdf)

    mock_chown.assert_called_once_with(output_dir, -1, 4242)
    mode = output_dir.stat().st_mode
    assert mode & 0o777 == 0o775


def test_compile_resume_pdf_falls_back_to_world_writable_dir_outside_container(
    sample_resume_data, tmp_path
) -> None:
    """Verify a missing _RENDER_USER group (e.g. local/test runs) still works.

    Falls back to 0o777 rather than failing the whole render when the
    container-specific group doesn't exist or can't be chown'd to. Mocks
    grp.getgrnam to force this path explicitly rather than relying on the
    ambient test environment lacking a "pptruser" group -- CI's own
    `validate` job runs inside the actual runtime image (ghcr.io/menil/
    jobgitops:latest), which does have a real pptruser group, so that
    assumption silently doesn't hold everywhere this suite runs.
    """
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)

    output_dir = tmp_path / "resumes"
    output_dir.mkdir(mode=0o700)
    output_pdf = output_dir / "resume.pdf"

    with (
        mock.patch(
            "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
        ),
        mock.patch(
            "jobgitops.renderer.grp.getgrnam", side_effect=KeyError("no such group")
        ),
    ):
        compile_resume_pdf(resume_json, "some-theme", output_pdf)

    mode = output_dir.stat().st_mode
    assert mode & 0o777 == 0o777


def test_compile_resume_pdf_uses_bare_theme_name(sample_resume_data, tmp_path) -> None:
    """Verify the theme argument passed to resumed is not the pinned install spec."""
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)
    output_pdf = tmp_path / "resume.pdf"

    with mock.patch(
        "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
    ) as mock_run:
        # Caller passes the bare name resolved by JobGitOps-4dk/9ru, never a
        # pinned "name@version" spec -- resumed's import() doesn't take one.
        compile_resume_pdf(
            resume_json, "@jsonresume/jsonresume-theme-professional", output_pdf
        )

    inner_command = mock_run.call_args.args[0][5]
    assert "@jsonresume/jsonresume-theme-professional@" not in inner_command


def test_compile_resume_pdf_only_passes_allowed_env_vars(
    sample_resume_data, tmp_path, monkeypatch
) -> None:
    """Verify only the env allowlist reaches the dropped-privilege subprocess.

    An allowlist, not a denylist of known secrets: a denylist silently leaks
    any secret nobody thought to add to it -- confirmed during review, when
    CLAUDE_API_KEY/ANTHROPIC_API_KEY (llm.py) and TAVILY_API_KEY/
    BRAVE_API_KEY/JINA_API_KEY (web.py) turned out to be missing from an
    earlier denylist version of this filter despite being real secrets used
    elsewhere in this codebase.
    """
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)
    output_pdf = tmp_path / "resume.pdf"

    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("GH_PAT", "secret-pat")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-openrouter")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "secret-claude-api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-anthropic")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-tavily")
    monkeypatch.setenv("BRAVE_API_KEY", "secret-brave")
    monkeypatch.setenv("JINA_API_KEY", "secret-jina")
    monkeypatch.setenv("SOME_FUTURE_SECRET_NOBODY_ALLOWLISTED_YET", "should-not-leak")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

    with mock.patch(
        "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
    ) as mock_run:
        compile_resume_pdf(resume_json, "some-theme", output_pdf)

    passed_env = mock_run.call_args.kwargs["env"]
    assert set(passed_env) <= _ALLOWED_ENV_VARS
    assert passed_env.get("PATH") == "/usr/local/bin:/usr/bin"
    assert "GITHUB_TOKEN" not in passed_env
    assert "GH_PAT" not in passed_env
    assert "GEMINI_API_KEY" not in passed_env
    assert "OPENROUTER_API_KEY" not in passed_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in passed_env
    assert "CLAUDE_API_KEY" not in passed_env
    assert "ANTHROPIC_API_KEY" not in passed_env
    assert "TAVILY_API_KEY" not in passed_env
    assert "BRAVE_API_KEY" not in passed_env
    assert "JINA_API_KEY" not in passed_env
    assert "SOME_FUTURE_SECRET_NOBODY_ALLOWLISTED_YET" not in passed_env


def test_compile_resume_pdf_raises_on_nonzero_exit(
    sample_resume_data, tmp_path, caplog
) -> None:
    """Verify a failing resumed invocation raises without leaking raw output.

    The full stderr/stdout is logged (for operators reading Actions run
    logs), but the exception message itself stays generic, since callers
    (triage.py) post exception messages verbatim as GitHub issue comments --
    not an appropriate destination for raw Bun/Node/Chromium output.
    """
    resume = Resume.from_dict(sample_resume_data)
    resume_json = tmp_path / "resume.json"
    compile_resume_json(resume, resume_json)
    output_pdf = tmp_path / "resume.pdf"

    failure = _fake_completed_process(
        returncode=1, stderr="Could not load theme some-theme. Is it installed?"
    )
    with (
        mock.patch("jobgitops.renderer.subprocess.run", return_value=failure),
        pytest.raises(RuntimeError) as exc_info,
    ):
        compile_resume_pdf(resume_json, "some-theme", output_pdf)

    assert "Could not load theme" not in str(exc_info.value)
    assert "exit 1" in str(exc_info.value)
    assert "Could not load theme" in caplog.text


def test_compile_resume_pdf_missing_json_file(tmp_path) -> None:
    """Verify compile_resume_pdf raises FileNotFoundError for a missing input file."""
    missing_json = tmp_path / "does-not-exist.json"
    output_pdf = tmp_path / "resume.pdf"

    with pytest.raises(FileNotFoundError, match="Resume JSON file not found at"):
        compile_resume_pdf(missing_json, "some-theme", output_pdf)


def test_compile_resume_full_pipeline(sample_resume_data, tmp_path) -> None:
    """Verify compile_resume writes JSON first, then compiles the PDF from that file."""
    resume = Resume.from_dict(sample_resume_data)
    output_pdf = tmp_path / "resume.pdf"
    output_json = tmp_path / "resume.json"

    assert not output_pdf.exists()
    assert not output_json.exists()

    with mock.patch(
        "jobgitops.renderer.subprocess.run", return_value=_fake_completed_process()
    ) as mock_run:
        compile_resume(
            resume, "@jsonresume/jsonresume-theme-professional", output_pdf, output_json
        )

    # The JSON file is real (compile_resume_json isn't mocked); the PDF
    # export subprocess is mocked, so no actual resumed/Chromium run happens
    # here -- that's covered by build-runner.yml's Docker-level smoke test.
    assert output_json.is_file()
    with output_json.open(encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == resume.to_dict()

    mock_run.assert_called_once()
    inner_command = mock_run.call_args.args[0][5]
    assert str(output_json.resolve()) in inner_command
    assert str(output_pdf.resolve()) in inner_command
