"""Fit score grade boundary constants for JobGitOps triage tiers.

Scores are on a 1.0-5.0 scale. Kept in their own module so both the triage
coordinator (src/triage.py) and the settings schema (jobgitops/schema.py) can
reference them without creating a circular import (schema <- loader <- triage).
"""

FIT_GRADE_A_PLUS_MIN = 4.5
FIT_GRADE_A_MIN = 4.0
FIT_GRADE_B_MIN = 3.5
