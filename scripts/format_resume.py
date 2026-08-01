"""Normalize resumes/resume.yaml to the canonical JobGitOps format.

Usage:
    python scripts/format_resume.py [PATH]           # rewrite in place
    python scripts/format_resume.py [PATH] --check   # fail if not canonical

PATH defaults to resumes/resume.yaml relative to the repository root.
"""

import argparse
import pathlib
import sys

from jobgitops.loader import load_resume, render_resume_yaml, resume_yaml_is_canonical

RESUME_PATH = pathlib.Path(__file__).resolve().parents[1] / "resumes" / "resume.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=RESUME_PATH,
        type=pathlib.Path,
        help="Resume YAML file to normalize (default: %(default)s).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify canonical formatting without writing (exit 1 if not canonical).",
    )
    args = parser.parse_args(argv)

    resume_path = args.path
    canonical = render_resume_yaml(load_resume(resume_path))
    current = resume_path.read_text(encoding="utf-8")

    if args.check:
        if not resume_yaml_is_canonical(resume_path):
            print(
                f"{resume_path} is not in canonical format; "
                "run `just format-resume` and commit the result."
            )
            return 1
        print(f"{resume_path} is in canonical format.")
        return 0

    if current != canonical:
        resume_path.write_text(canonical, encoding="utf-8")
        print(f"Normalized {resume_path}.")
    else:
        print(f"{resume_path} already canonical.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
