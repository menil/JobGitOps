# AI Decision Record: Gmail Integration Technical Specification

## Context & Goal

JobGitOps tracks job applications via GitHub Issues; moving an issue's
status today requires the user to either hand-apply a label or type a
conversational intent as a comment ("I applied", "they rejected me"). Most
of that signal, however, already arrives automatically as email (ATS
application confirmations, rejections, interview invites) that nothing
currently reads.

Goal: produce a design-only technical specification (no code) for an
optional Gmail integration that detects these emails and drives the
existing `status_update` side-effect path — without building a second,
parallel side-effect system, without new mandatory dependencies, and
without expanding what the integration can access beyond a Gmail label the
user explicitly controls.

Constraints: fork-and-run distribution model (ships via the shared
container image plus a template repo sync that runs on a separate
schedule), must stay free-by-default, must not introduce a new label
taxonomy or Projects V2 columns, must not weaken the existing bot-loop
guard that prevents infinite comment→respond→comment loops, and must
remain fully optional and backward compatible for repos that don't opt in.

## Architecture & Key Decisions

1. **Gmail API client**: the official `google-api-python-client` +
   `google-auth`, not hand-rolled `urllib.request` (unlike
   `github_client.py`'s GitHub REST calls). Verified directly against
   `uv.lock` that both packages are already fully resolved as transitive
   dependencies of the existing `google-generativeai` package, so adopting
   them adds zero new packages to the container image.
2. **Matching**: a single `EMAIL_MATCH_PROMPT` LLM call per email, given
   the email content plus a candidate list drawn from open,
   actively-pursued issues (`ready-to-apply`/`applied`/`in-loop`/
   `offer-received`). A cheap deterministic pre-filter (URL/domain match
   against a candidate's stored apply URL, or a company-name substring
   match) narrows which candidates the LLM sees, but never resolves a
   match by itself — even a pre-filter-narrowed single candidate still
   goes through the LLM before any status or label change executes.
3. **Sync mechanism**: every hourly run re-queries Gmail for
   `label:<configured label> newer_than:{days_back}d`, deduplicating via a
   `{message_id: date}` map pruned once entries age out of the
   `days_back` window — not a Gmail `historyId` incremental-sync cursor.
4. **Side effects**: a resolved match executes through the existing
   `respond.execute_action` unchanged — same label sync, same terminal
   auto-close, same Projects V2 update, same confirmation-comment marker
   convention — so Gmail-triggered and comment-triggered status changes
   are indistinguishable to the rest of the system.
5. **Cursor storage**: the sync cursor (`data/gmail-state.json`) is
   committed to a dedicated orphan branch (`gmail-sync-state`), not
   `main`, matching the existing precedent that automated writes
   (tailored-resume commits) never land on `main`.
6. **Security hardening added during review**: the DMARC check is pinned
   to Gmail's own trusted `authserv-id` rather than an unscoped substring
   search; a second hidden HTML comment marker
   (`<!-- jobgitops:gmail-notice -->`) plus an extension to `respond.py`'s
   bot-loop guard prevents an ambiguous-match heads-up comment from
   re-triggering the conversational assistant; a real comment-dedup check
   runs before posting (since `GitHubClient.post_comment` is not
   idempotent); and the spec now accurately describes the Gmail label
   filter as an application-logic boundary, not a credential-level one
   (`gmail.readonly` grants the whole mailbox; there is no finer-grained
   Gmail scope that still allows reading message bodies).

## Alternatives Considered & Rejected

- **Two-phase matching** (a deterministic fast path that could resolve a
  match with zero LLM involvement, falling back to a separate
  extraction+disambiguation LLM pair only when that didn't resolve
  uniquely): rejected after review surfaced two problems. First, it was
  spoofable — DMARC authenticates the sending domain, not the free-text
  display name, so a forged display name could resolve an issue with no
  model review at all. Second, it had a real coherence bug — the
  "ambiguous, 2+ hits" fallback case discarded the first phase's own
  candidate computation and recomputed a possibly different one from
  scratch via a fresh LLM extraction call, so the two phases weren't
  guaranteed to agree. Collapsing to one always-invoked LLM call, with the
  deterministic check demoted to a pure candidate-list pre-filter, removes
  both problems and cuts the design from three LLM prompts to one.
- **Gmail `historyId` incremental sync** (with expiry detection, a
  query-based fallback, and a rebaseline step): rejected as machinery
  sized for a high-volume mailbox, not this feature's actual scale (one
  person, one label, hourly cron). A fixed lookback query re-run every
  hour, deduplicated by an age-pruned map, produces the same observable
  behavior with far less state/expiry logic, and — as a side effect —
  makes the `query` config knob apply uniformly instead of only on a rare
  fallback path.
- **Hand-rolled Gmail REST calls over stdlib `urllib.request`**
  (mirroring `github_client.py`): initially chosen for consistency with
  that existing pattern and to avoid a perceived new dependency. Rejected
  once verified against `uv.lock` that the official
  `google-api-python-client` (and `google-auth`) are already fully
  resolved transitively — the "avoid a new dependency" premise was
  factually wrong, and GitHub, unlike Gmail, has no first-party official
  Python client, so the precedent doesn't actually transfer.
- **Committing the sync cursor to `main` with a `[skip ci]` marker**:
  considered to avoid spuriously re-triggering unfiltered
  `push`-triggered workflows, but rejected in favor of a dedicated orphan
  branch, which sidesteps that interaction by construction and matches
  how tailored-resume application branches already keep automated writes
  off `main`.
- **A fixed-size ring buffer for the de-dup cursor** (e.g. the most
  recent 500 message IDs): rejected in favor of pruning by message age
  against `days_back`, since a fixed count is an arbitrary guess that
  could evict a still-in-window message ID under high volume, while
  age-based pruning is provably safe at any volume and scales with actual
  mailbox activity instead of a guessed constant.
