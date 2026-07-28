"""YAML file loading and validation helpers for JobGitOps."""

import pathlib

import yaml

from jobgitops.schema import Resume, Settings, ValidationError


def load_settings(path: str | pathlib.Path) -> Settings:
    """Load settings configuration from a YAML file.

    If the file does not exist, returns default settings.
    Raises ValidationError if YAML parsing or schema validation fails.
    """
    file_path = pathlib.Path(path)
    if not file_path.is_file():
        # Fall back cleanly to default Settings if no settings file exists
        return Settings()

    try:
        with file_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML syntax in settings: {e}") from e
    except Exception as e:
        raise ValidationError(f"Failed to read settings file: {e}") from e

    # If the settings file is empty, return defaults
    if data is None:
        return Settings()

    return Settings.from_dict(data)


def load_resume(path: str | pathlib.Path) -> Resume:
    """Load resume data from a YAML file.

    Raises FileNotFoundError if the file does not exist.
    Raises ValidationError if YAML parsing or schema validation fails.
    """
    file_path = pathlib.Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Resume file not found: {file_path}")

    try:
        with file_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML syntax in resume: {e}") from e
    except Exception as e:
        raise ValidationError(f"Failed to read resume file: {e}") from e

    if not isinstance(data, dict):
        raise ValidationError("Resume YAML content must represent a dictionary.")

    return Resume.from_dict(data)
