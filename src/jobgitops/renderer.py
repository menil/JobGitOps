"""Resume compilation pipeline using the JSON Resume `resumed` CLI."""

import grp
import json
import logging
import os
import pathlib
import shlex
import subprocess

from jobgitops.schema import Resume

logger = logging.getLogger("jobgitops.renderer")

# Dropped-privilege account baked into the runtime image (see Dockerfile).
# Chromium's own sandbox cannot be enabled in this container runtime (Docker's
# default seccomp/namespace restrictions apply regardless of caller UID), so
# `--puppeteer-arg=--no-sandbox` is still required below; running as this
# unprivileged user instead of root still meaningfully narrows the blast
# radius of a Chromium renderer compromise.
_RENDER_USER = "pptruser"

# Absolute paths, not bare command names: `su -s /bin/sh <user> -c` does not
# reliably inherit the caller's PATH, and resumed's global install lives
# outside any user's home directory (see BUN_INSTALL=/opt/bun in the
# Dockerfile) specifically so `_RENDER_USER` can read it.
_BUN_BIN = "/usr/local/bin/bun"
_RESUMED_BIN = "/opt/bun/bin/resumed"

# Only these pass through to the rendering subprocess -- everything else
# (all API keys/tokens included) is dropped by default. Chosen as an
# allowlist rather than a denylist of known secrets: a denylist silently
# leaks any secret nobody thought to add to it (confirmed during review --
# CLAUDE_API_KEY/ANTHROPIC_API_KEY in llm.py and TAVILY_API_KEY/
# BRAVE_API_KEY/JINA_API_KEY in web.py were all missing from an earlier
# denylist version of this set), whereas a new secret is safe by default
# against an allowlist. `su` resets HOME/USER/LOGNAME to _RENDER_USER's own
# passwd entry regardless of what's passed here, so those don't need to be
# listed. Dropping privilege to _RENDER_USER does not by itself stop
# environment-variable inheritance -- a child process still gets whatever
# env is passed by default -- so this matters as defense in depth around
# Chromium rendering resume content (ultimately derived from scraped job
# postings and LLM-tailored text, i.e. not fully trusted input).
_ALLOWED_ENV_VARS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "BUN_INSTALL",
        "PUPPETEER_EXECUTABLE_PATH",
        "PUPPETEER_SKIP_DOWNLOAD",
    }
)


def compile_resume_pdf(
    resume_json_path: str | pathlib.Path,
    theme_name: str,
    output_pdf_path: str | pathlib.Path,
) -> None:
    """Compile a JSON Resume file to a PDF using the `resumed` CLI.

    Args:
        resume_json_path: Path to an existing JSON Resume file (see
            compile_resume_json).
        theme_name: Bare installed theme package name (e.g.
            "@jsonresume/jsonresume-theme-professional") -- NOT a pinned
            "name@version"/"name#sha" install spec. resumed's module
            resolution looks the package up by name in the tree it's
            installed in; it doesn't take a version.
        output_pdf_path: Output target path for the compiled PDF file.

    Raises:
        FileNotFoundError: If resume_json_path does not exist.
        RuntimeError: If the resumed subprocess exits non-zero.
    """
    resume_json_path = pathlib.Path(resume_json_path)
    output_pdf_path = pathlib.Path(output_pdf_path)

    if not resume_json_path.is_file():
        raise FileNotFoundError(f"Resume JSON file not found at: {resume_json_path}")

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # The directory above is typically created by the caller (e.g. triage.py's
    # git checkout handling) as root, but resumed itself runs as the
    # unprivileged _RENDER_USER below and would otherwise get EACCES writing
    # the output PDF into a directory it doesn't own. Prefer handing the
    # directory's group to _RENDER_USER's own group and making it group-
    # writable (0o775) over a blanket world-writable 0o777. Falls back to
    # 0o777 when that's not possible -- e.g. _RENDER_USER's group doesn't
    # exist (running outside the container, such as local dev/test) or we
    # lack permission to chown (not running as root there either).
    try:
        render_user_gid = grp.getgrnam(_RENDER_USER).gr_gid
        os.chown(output_pdf_path.parent, -1, render_user_gid)
        output_pdf_path.parent.chmod(0o775)
    except (KeyError, PermissionError, OSError):
        output_pdf_path.parent.chmod(0o777)

    # NOT `bunx resumed export`: bunx re-resolves resumed into an isolated
    # per-invocation cache on every call and cannot see the globally-
    # installed theme/puppeteer siblings baked into the image, so it fails
    # with "Could not load theme ... Is it installed?" even when the theme
    # really is installed (confirmed by hands-on testing -- see Dockerfile).
    resumed_command = [
        _BUN_BIN,
        _RESUMED_BIN,
        "export",
        str(resume_json_path.resolve()),
        "-t",
        theme_name,
        "-o",
        str(output_pdf_path.resolve()),
        "--puppeteer-arg=--no-sandbox",
    ]

    filtered_env = {
        key: value for key, value in os.environ.items() if key in _ALLOWED_ENV_VARS
    }

    result = subprocess.run(
        ["su", "-s", "/bin/sh", _RENDER_USER, "-c", shlex.join(resumed_command)],
        env=filtered_env,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        # Full output goes to logs only -- it's raw Bun/Node/Chromium output
        # that could echo container filesystem details, and callers (e.g.
        # triage.py) surface exception messages in GitHub issue comments,
        # which isn't an appropriate destination for that level of detail.
        logger.error(
            "resumed export failed (exit %d): %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        raise RuntimeError(
            f"resumed export failed (exit {result.returncode}). "
            "See the workflow run logs for details."
        )


def compile_resume_json(
    resume: Resume,
    output_json_path: str | pathlib.Path,
) -> None:
    """Serialize a Resume object to a standard JSON Resume file.

    Args:
        resume: The parsed Resume instance to serialize.
        output_json_path: Output target path for the JSON file.
    """
    output_json_path = pathlib.Path(output_json_path)

    # Ensure target output directory exists before writing
    output_json_path.parent.mkdir(parents=True, exist_ok=True)

    serialized_data = resume.to_dict()

    with output_json_path.open("w", encoding="utf-8") as f:
        # indent=2 and ensure_ascii=False keeps the generated JSON resume clean,
        # human-readable, and properly formatted with UTF-8 characters.
        json.dump(serialized_data, f, indent=2, ensure_ascii=False)


def compile_resume(
    resume: Resume,
    theme_name: str,
    output_pdf_path: str | pathlib.Path,
    output_json_path: str | pathlib.Path,
) -> None:
    """Compile both the JSON and PDF representations of the resume.

    The JSON file is written first since resumed's PDF export reads it from
    disk as its input.

    Args:
        resume: The parsed Resume instance.
        theme_name: Bare installed theme package name (see compile_resume_pdf).
        output_pdf_path: Output target path for the compiled PDF.
        output_json_path: Output target path for the JSON resume.
    """
    compile_resume_json(resume, output_json_path)
    compile_resume_pdf(output_json_path, theme_name, output_pdf_path)
