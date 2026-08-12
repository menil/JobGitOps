"""CLI entry point for validating and checking the resume format."""

import argparse
import pathlib
import sys

from jobgitops.loader import load_resume
from jobgitops.schema import ValidationError


def main(argv: list[str] | None = None) -> int:
    """Validate a resume YAML file against JSON Resume schema and check formatting."""
    parser = argparse.ArgumentParser(description="Validate a resume YAML file.")
    parser.add_argument(
        "path",
        type=pathlib.Path,
        help="Path to the resume YAML file.",
    )
    parser.add_argument(
        "--check-canonical",
        action="store_true",
        help=(
            "Verify the file is canonically formatted. "
            "Exits with 2 if valid but not canonical."
        ),
    )
    args = parser.parse_args(argv)

    try:
        resume = load_resume(args.path)
        if args.check_canonical:
            from jobgitops.loader import render_resume_yaml

            canonical = render_resume_yaml(resume)
            current = args.path.read_text(encoding="utf-8")
            if current != canonical:
                print(
                    f"Resume {args.path} is valid but not canonically formatted.",
                    file=sys.stderr,
                )
                return 2
            print(f"Resume {args.path} is valid and canonically formatted.")
        else:
            print(f"Resume {args.path} is valid.")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValidationError as e:
        print(f"Schema Validation Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected Error reading resume: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
