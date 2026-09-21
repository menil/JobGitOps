"""Unit tests for the Gmail-integration deterministic pre-filter (spec
`specs/gmail-integration.md` §6.1), which narrows -- but must never itself
resolve -- the candidate list handed to the (separate) LLM match call.
"""

import pytest

from jobgitops.gmail_match import Candidate, PreFilterResult, prefilter_candidates


def _candidate(
    number: int,
    company: str = "Acme Corp",
    apply_url: str = "https://boards.greenhouse.io/acme-corp/jobs/1",
    title: str = "Staff Engineer",
    role: str = "Staff Engineer",
) -> Candidate:
    return Candidate(
        number=number,
        title=title,
        company=company,
        role=role,
        apply_url=apply_url,
    )


# --- Tiering (spec §6.1's table) -------------------------------------------


def test_zero_hit_tier_returns_full_unfiltered_pool_unchanged() -> None:
    """Zero hits must return the FULL pool, not an empty list -- callers
    get the same coverage as if no pre-filter had run at all."""
    pool = [
        _candidate(1, company="Acme Corp", apply_url="https://acme.com/jobs/1"),
        _candidate(2, company="Widget LLC", apply_url="https://widget.com/jobs/2"),
        _candidate(3, company="Globex Inc", apply_url="https://globex.com/jobs/3"),
    ]

    result = prefilter_candidates(
        email_sender="notifications@totally-unrelated-domain.example",
        email_subject="Your weekly newsletter",
        email_body="Nothing relevant here, just a newsletter blurb.",
        candidates=pool,
    )

    assert isinstance(result, PreFilterResult)
    assert result.tier == "zero_hit"
    assert result.candidates == pool
    assert len(result.candidates) == 3


def test_one_hit_tier_returns_only_the_single_matching_candidate() -> None:
    pool = [
        _candidate(1, company="Acme Corp", apply_url="https://acme.com/jobs/1"),
        _candidate(2, company="Widget LLC", apply_url="https://widget.com/jobs/2"),
    ]

    result = prefilter_candidates(
        email_sender="notify@acme.com",
        email_subject="Update on your Acme Corp application",
        email_body="See https://acme.com/jobs/1 for the latest status.",
        candidates=pool,
    )

    assert result.tier == "one_hit"
    assert [c.number for c in result.candidates] == [1]


def test_multi_hit_tier_returns_only_the_narrowed_hit_subset() -> None:
    """Two candidates hit via two different, independent signals (a URL hit
    for one, a company-string hit for the other) -- both narrow into the
    multi-hit subset; the untouched third candidate is dropped."""
    pool = [
        _candidate(1, company="Beta Corp", apply_url="https://beta.com/jobs/1"),
        _candidate(2, company="Acme Corp", apply_url="https://widget.com/jobs/2"),
        _candidate(3, company="Gamma LLC", apply_url="https://gamma.com/jobs/3"),
    ]

    result = prefilter_candidates(
        email_sender="notify@unrelated.example",
        email_subject="Update from Acme Corp",
        email_body="See https://beta.com/jobs/1 for status.",
        candidates=pool,
    )

    assert result.tier == "multi_hit"
    assert {c.number for c in result.candidates} == {1, 2}


def test_zero_hit_tier_with_large_pool_is_not_confused_with_multi_hit() -> None:
    """Regression: a bare list of size N is ambiguous between "zero-hit,
    pool of size N" and "N-hit multi-hit" -- the tier field must disambiguate.
    """
    pool = [
        _candidate(n, company=f"Company{n}", apply_url=f"https://company{n}.com/j/1")
        for n in range(1, 6)
    ]

    result = prefilter_candidates(
        email_sender="noreply@unrelated.example",
        email_subject="No match here",
        email_body="Nothing relevant.",
        candidates=pool,
    )

    assert result.tier == "zero_hit"
    assert len(result.candidates) == 5


# --- URL/domain hit: host + path-segment boundary correctness --------------


def test_url_hit_on_matching_host_and_path_segments() -> None:
    candidate = _candidate(1, apply_url="https://boards.greenhouse.io/acme-corp")

    result = prefilter_candidates(
        email_sender="notifications@greenhouse.io",
        email_subject="Application received",
        email_body="Thanks for applying: https://boards.greenhouse.io/acme-corp",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"
    assert result.candidates == [candidate]


def test_url_hit_is_case_insensitive_on_host_and_path() -> None:
    candidate = _candidate(1, apply_url="https://Boards.Greenhouse.io/Acme-Corp")

    result = prefilter_candidates(
        email_sender="notify@example.com",
        email_subject="No company-string signal here",
        email_body="Link: https://BOARDS.GREENHOUSE.IO/ACME-CORP",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


def test_url_hit_does_not_false_positive_on_shared_first_segment() -> None:
    """Regression: acme.com/jobs/123 must NOT match acme.com/jobs/1234 --
    both share host "acme.com" and first path segment "jobs", but a naive
    first-segment-only (or raw string-prefix) comparison would incorrectly
    treat these as the same posting. Full path-segment equality is required.
    """
    candidate = _candidate(
        1,
        company="Unrelated Co",
        apply_url="https://acme.com/jobs/123",
    )

    result = prefilter_candidates(
        email_sender="notify@somewhere-else.example",
        email_subject="No company-string signal here",
        email_body="Check this posting: https://acme.com/jobs/1234",
        candidates=[candidate],
    )

    assert result.tier == "zero_hit"
    assert result.candidates == [candidate]


def test_url_hit_string_prefix_is_not_naively_matched() -> None:
    """Regression: one apply_url being a raw string-prefix of a body URL
    must not itself cause a hit (see boundary test above); this exercises
    the same guarantee from the apply_url-is-the-longer-string direction.
    """
    candidate = _candidate(
        1,
        company="Unrelated Co",
        apply_url="https://acme.com/jobs/1234",
    )

    result = prefilter_candidates(
        email_sender="notify@somewhere-else.example",
        email_subject="No company-string signal here",
        email_body="Check this posting: https://acme.com/jobs/123",
        candidates=[candidate],
    )

    assert result.tier == "zero_hit"


def test_url_hit_via_sender_domain_when_body_has_no_url() -> None:
    candidate = _candidate(1, apply_url="https://acme.com")

    result = prefilter_candidates(
        email_sender="Acme Careers <careers@acme.com>",
        email_subject="No company-string signal",
        email_body="No links in this email body at all.",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


def test_url_extraction_ignores_trailing_sentence_punctuation() -> None:
    candidate = _candidate(1, apply_url="https://acme.com/jobs/42")

    result = prefilter_candidates(
        email_sender="notify@somewhere-else.example",
        email_subject="No company-string signal here",
        email_body="See https://acme.com/jobs/42. Good luck!",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


def test_url_extraction_stops_at_closing_paren() -> None:
    candidate = _candidate(1, apply_url="https://acme.com/jobs/42")

    result = prefilter_candidates(
        email_sender="notify@somewhere-else.example",
        email_subject="No company-string signal here",
        email_body="(see https://acme.com/jobs/42) for the listing",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


# --- Company-string hit: normalization + false-positive regression --------


@pytest.mark.parametrize(
    "company,sender",
    [
        ("Acme Inc", "Recruiting Team <jobs@acme-inc-mail.example>"),
        ("Acme Inc.", "Recruiting Team <jobs@acme-inc-mail.example>"),
        ("Acme LLC", "Acme LLC Careers <careers@example.com>"),
        ("Acme L.L.C.", "Acme L.L.C. Careers <careers@example.com>"),
        ("Acme Corp", "Acme Corp Careers <careers@example.com>"),
        ("Acme Corp.", "Acme Corp. Careers <careers@example.com>"),
        ("Acme Corporation", "Acme Corporation Careers <careers@example.com>"),
    ],
)
def test_company_hit_strips_common_suffixes_case_insensitively(
    company: str, sender: str
) -> None:
    candidate = _candidate(1, company=company, apply_url="https://acme.com/careers")

    result = prefilter_candidates(
        email_sender=sender,
        email_subject="An update",
        email_body="No URLs in this body.",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


def test_company_hit_matches_in_subject_too() -> None:
    candidate = _candidate(1, company="Acme Corp", apply_url="https://acme.com/careers")

    result = prefilter_candidates(
        email_sender="notify@unrelated.example",
        email_subject="Your Acme Corp application has been updated",
        email_body="No URLs here.",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


def test_company_hit_false_positive_regression_short_name_substring() -> None:
    """Regression: candidate company "Wave" must not falsely match an
    unrelated sender display name like "Waveform Recruiting" -- a raw
    substring check would match ("wave" is a substring of "waveform"), but
    word-boundary-aware matching correctly rejects it.
    """
    candidate = _candidate(
        1,
        company="Wave",
        apply_url="https://wave.com/jobs/1",
    )

    result = prefilter_candidates(
        email_sender="Waveform Recruiting <jobs@waveform-recruiting.example>",
        email_subject="A great opportunity for you",
        email_body="No links here.",
        candidates=[candidate],
    )

    assert result.tier == "zero_hit"
    assert result.candidates == [candidate]


def test_company_hit_word_boundary_still_matches_genuine_short_name() -> None:
    """Companion to the false-positive regression above: word-boundary
    matching must still find a genuine hit for the same short company name
    when it appears as a standalone word.
    """
    candidate = _candidate(
        1,
        company="Wave",
        apply_url="https://wave.com/jobs/1",
    )

    result = prefilter_candidates(
        email_sender="Wave Careers <careers@wave.com>",
        email_subject="Update on your Wave application",
        email_body="No links here.",
        candidates=[candidate],
    )

    assert result.tier == "one_hit"


# --- Purity: no side effects, no I/O -----------------------------------


def test_prefilter_candidates_is_pure_and_does_not_mutate_inputs() -> None:
    pool = [
        _candidate(1, company="Acme Corp", apply_url="https://acme.com/jobs/1"),
        _candidate(2, company="Widget LLC", apply_url="https://widget.com/jobs/2"),
    ]
    pool_snapshot = list(pool)

    result_a = prefilter_candidates(
        email_sender="careers@acme.com",
        email_subject="Update from Acme Corp",
        email_body="See https://acme.com/jobs/1",
        candidates=pool,
    )
    result_b = prefilter_candidates(
        email_sender="careers@acme.com",
        email_subject="Update from Acme Corp",
        email_body="See https://acme.com/jobs/1",
        candidates=pool,
    )

    assert pool == pool_snapshot
    assert result_a.tier == result_b.tier
    assert result_a.candidates == result_b.candidates
