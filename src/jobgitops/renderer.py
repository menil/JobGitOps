"""Resume compilation pipeline using the JSON Resume `resumed` CLI."""

import grp
import json
import logging
import os
import pathlib
import shlex
import shutil
import subprocess

from jobgitops.schema import Resume, theme_looks_pinned

logger = logging.getLogger("jobgitops.renderer")


class ThemeInstallError(Exception):
    """Raised when a JSON Resume theme package can't be installed or used."""


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
# against an allowlist. For the render step, `su` resets HOME/USER/LOGNAME
# to _RENDER_USER's own passwd entry regardless of what's passed here, so
# HOME isn't needed there -- but ensure_theme_installed's install/interface-
# check subprocesses run as root directly, with no `su` to reset it, and
# `bun add -g`/`bun -e` need a real HOME to resolve their own config/cache
# locations (e.g. ~/.bunfig.toml lookups), so it's included for that path.
# Dropping privilege to _RENDER_USER does not by itself stop
# environment-variable inheritance -- a child process still gets whatever
# env is passed by default -- so this matters as defense in depth around
# Chromium rendering resume content (ultimately derived from scraped job
# postings and LLM-tailored text, i.e. not fully trusted input).
_ALLOWED_ENV_VARS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "BUN_INSTALL",
        "PUPPETEER_EXECUTABLE_PATH",
        "PUPPETEER_SKIP_DOWNLOAD",
        "FONTCONFIG_FILE",
        "NIX_SSL_CERT_FILE",
        "SSL_CERT_FILE",
        "NIX_CONFIG",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


def _filtered_env() -> dict[str, str]:
    """Build a subprocess environment limited to _ALLOWED_ENV_VARS.

    Used for every subprocess this module launches that runs third-party
    code (resumed/Chromium rendering, and theme install/interface-check
    below) -- not just the unprivileged render step. `bun add -g` and the
    `bun -e` interface check both run as root and, for the latter,
    `import()` the theme package's own module-level code -- an unfiltered
    environment there would hand every secret in the process to that
    third-party code before privilege is ever dropped.
    """
    return {key: value for key, value in os.environ.items() if key in _ALLOWED_ENV_VARS}


# Where `bun add -g` actually places installed packages, given
# BUN_INSTALL=/opt/bun in the Dockerfile (bun's default is ~/.bun instead).
_BUN_GLOBAL_NODE_MODULES = pathlib.Path("/opt/bun/install/global/node_modules")


def _theme_bare_name(theme_spec: str) -> str | None:
    """Return the bare installed package name for an npm-style spec.

    Returns None for a "github:" spec, whose real package name (read from
    its own package.json, not necessarily matching the repo name) isn't
    known until after it's installed -- see _parse_installed_theme_name.
    """
    if theme_spec.startswith("github:"):
        return None
    # Version is everything after the LAST "@" -- scoped packages like
    # "@scope/name@version" have two "@"s, and a bare scoped name with no
    # version at all (just "@scope/name") has exactly one, at index 0, which
    # must NOT be treated as a version separator -- hence requiring `name`
    # to be non-empty too, not just `sep`.
    name, sep, _version = theme_spec.rpartition("@")
    return name if sep and name else theme_spec


def _parse_installed_theme_name(bun_stdout: str, theme_spec: str) -> str:
    """Extract the resolved package name from `bun add -g`'s own output.

    Needed for "github:" specs, where bun reports a line like "installed
    <name>@github:<owner>/<repo>#<sha>" -- <name> comes from the installed
    package's own package.json and may not match the repo name. Confirmed by
    hands-on testing that a plain rpartition("@") on this line correctly
    isolates <name> in both the npm-version and github-spec cases, since
    neither a semver version nor a "github:owner/repo#sha" string itself
    contains an "@", and bun's own report always includes a real version/
    spec suffix after the name (unlike an arbitrary user-supplied spec,
    which is why _theme_bare_name needs the extra bare-scoped-name check
    above and this function doesn't).
    """
    for line in bun_stdout.splitlines():
        line = line.strip()
        if line.startswith("installed "):
            spec_part = line.removeprefix("installed ").split(" with binaries:")[0]
            name, sep, _rest = spec_part.rpartition("@")
            if sep:
                return name
    raise ThemeInstallError(
        f"Could not determine the installed package name for theme "
        f"{theme_spec!r} from bun's output."
    )


def _theme_is_installed(bare_name: str) -> bool:
    return (_BUN_GLOBAL_NODE_MODULES / bare_name).is_dir()


def _validate_theme_interface(bare_name: str) -> None:
    """Confirm the installed package actually exports a render(resume) fn.

    Catches a non-conforming package (or a bad install) here, with a clear
    error, instead of a confusing failure deep inside a later `resumed
    export` run.
    """
    check_script = (
        f"import({json.dumps(bare_name)})"
        ".then(m => process.exit(typeof m.render === 'function' ? 0 : 1))"
        ".catch(e => { console.error(e); process.exit(2); })"
    )
    result = subprocess.run(
        [_BUN_BIN, "-e", check_script],
        # `bun -e` resolves modules relative to its CWD (like a script file
        # would), not relative to _BUN_GLOBAL_NODE_MODULES -- confirmed by
        # hands-on testing that without this, a theme with its own
        # dependencies (e.g. the default theme's react/react-dom) fails to
        # resolve them when this runs from an arbitrary caller CWD (e.g.
        # /workspace, jobgitops' own directory, which has no relation to the
        # installed theme's dependency tree).
        cwd=_BUN_GLOBAL_NODE_MODULES,
        env=_filtered_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Full output (e.g. why the import actually failed) goes to logs
        # only -- see the identical rationale on compile_resume_pdf's own
        # error path below, which this mirrors.
        logger.error(
            "Theme %r failed its render(resume) interface check: %s",
            bare_name,
            result.stderr.strip() or result.stdout.strip(),
        )
        raise ThemeInstallError(
            f"Installed theme {bare_name!r} does not export a usable "
            "render(resume) function -- it may not be a valid JSON Resume "
            "theme, or failed to load. See the workflow run logs for details."
        )


def ensure_theme_installed(theme_spec: str) -> str:
    """Ensure a JSON Resume theme is installed in the shared global Bun tree.

    Installs it via `bun add -g` if not already present, and returns its
    bare package name for use with compile_resume/compile_resume_pdf.

    Must run as root (or whoever owns /opt/bun): this writes into the shared
    global tree that compile_resume_pdf's rendering subprocess -- which runs
    as the unprivileged _RENDER_USER -- only ever reads from. Confirmed by
    hands-on testing that a runtime (not just Dockerfile-build-time) `bun add
    -g` run as root already produces world-readable files under the default
    umask (0o755 dirs / 0o644 files), so _RENDER_USER can read a
    freshly-installed theme with no extra chmod step needed here.

    Args:
        theme_spec: A pinned npm spec ("<package>@<version>") or GitHub spec
            ("github:<owner>/<repo>#<commit-sha>"), per
            template/config/settings.yaml's documented `theme` format.

    Returns:
        The bare installed package name (e.g.
        "@jsonresume/jsonresume-theme-professional") -- NOT the pinned spec
        passed in. Pass this straight to compile_resume/compile_resume_pdf.

    Raises:
        ThemeInstallError: If installation fails, or the installed package
            doesn't export a usable render(resume) function.
    """
    if not theme_looks_pinned(theme_spec):
        # Settings.from_dict() already rejects an unpinned theme read from
        # settings.yaml, so reaching this warning means either the hardcoded
        # _DEFAULT_RESUME_THEME fallback itself is unpinned (a real bug) or
        # a caller outside triage.py's normal path invoked this directly.
        logger.warning(
            "Theme spec %r is not pinned to an exact version or commit SHA. "
            "JobGitOps installs and executes this package's code at render "
            "time -- an unpinned spec can silently change what code runs "
            "between renders.",
            theme_spec,
        )

    bare_name = _theme_bare_name(theme_spec)
    # Relies on short-circuit evaluation: `_theme_is_installed(bare_name)` is
    # never reached (and never called with None) when bare_name is None.
    if bare_name is None or not _theme_is_installed(bare_name):
        logger.info("Installing resume theme %r", theme_spec)
        result = subprocess.run(
            [_BUN_BIN, "add", "-g", theme_spec],
            env=_filtered_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Full output goes to logs only -- see the identical rationale
            # on compile_resume_pdf's own error path below.
            logger.error(
                "Failed to install theme %r: %s",
                theme_spec,
                result.stderr.strip() or result.stdout.strip(),
            )
            raise ThemeInstallError(
                f"Failed to install theme {theme_spec!r}. "
                "See the workflow run logs for details."
            )

        if bare_name is None:
            bare_name = _parse_installed_theme_name(result.stdout, theme_spec)

    # Always validated, even on the already-installed fast path above: a
    # directory merely existing (e.g. a corrupted image layer, a partial
    # install left over from a previous run) is not proof the package is
    # usable, and this is the only thing that would otherwise catch that
    # before a confusing failure deep inside a later `resumed export` run.
    _validate_theme_interface(bare_name)

    return bare_name


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

    result = subprocess.run(
        ["su", "-s", "/bin/sh", _RENDER_USER, "-c", shlex.join(resumed_command)],
        env=_filtered_env(),
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


def generate_pdf_diff(
    base_pdf_path: str | pathlib.Path,
    tailored_pdf_path: str | pathlib.Path,
    output_diff_pdf_path: str | pathlib.Path,
    theme: str = "github",
    granularity: str = "word",
) -> pathlib.Path:
    """Generate a visual side-by-side diff PDF comparing base and tailored PDFs.

    Uses `pdf-vdiff` if present in PATH, or falls back to
    `nix run github:menil/pdf-vdiff`.

    Exit codes from pdf-vdiff:
        0: Documents are identical (diff PDF generated without highlights).
        1: Differences detected (diff PDF generated with highlights).
        2+: Fatal error (e.g. invalid arguments or missing input).

    Args:
        base_pdf_path: Path to the original base resume PDF.
        tailored_pdf_path: Path to the tailored resume PDF.
        output_diff_pdf_path: Target path for the generated diff PDF.
        theme: Highlight theme ('github', 'intellij', 'classic', 'high-contrast').
        granularity: Diff granularity ('word', 'line', 'character').

    Returns:
        The output diff PDF path as a pathlib.Path.

    Raises:
        FileNotFoundError: If base_pdf_path or tailored_pdf_path does not exist.
        RuntimeError: If neither runner is found or pdf-vdiff fails.
    """
    base = pathlib.Path(base_pdf_path)
    tailored = pathlib.Path(tailored_pdf_path)
    output = pathlib.Path(output_diff_pdf_path)

    if not base.is_file():
        raise FileNotFoundError(f"Base PDF not found: {base}")
    if not tailored.is_file():
        raise FileNotFoundError(f"Tailored PDF not found: {tailored}")

    output.parent.mkdir(parents=True, exist_ok=True)

    pdf_vdiff_bin = shutil.which("pdf-vdiff")
    if pdf_vdiff_bin:
        cmd = [
            pdf_vdiff_bin,
            str(base.resolve()),
            str(tailored.resolve()),
            "-o",
            str(output.resolve()),
            "--force",
            "--theme",
            theme,
            "--granularity",
            granularity,
        ]
    elif shutil.which("nix"):
        cmd = [
            "nix",
            "run",
            "github:menil/pdf-vdiff",
            "--",
            str(base.resolve()),
            str(tailored.resolve()),
            "-o",
            str(output.resolve()),
            "--force",
            "--theme",
            theme,
            "--granularity",
            granularity,
        ]
    else:
        raise RuntimeError(
            "Neither 'pdf-vdiff' nor 'nix' executable found in PATH to "
            "generate PDF diff."
        )

    logger.info("Running visual PDF diff: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        env=_filtered_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    # 0 = identical documents, 1 = differences found (both successfully generate output)
    if result.returncode not in (0, 1):
        err_msg = result.stderr.strip() or result.stdout.strip()
        logger.error("pdf-vdiff failed (exit %d): %s", result.returncode, err_msg)
        raise RuntimeError(f"pdf-vdiff failed (exit {result.returncode}): {err_msg}")

    return output
