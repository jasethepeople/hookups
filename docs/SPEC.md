# Product specification

**Status:** Draft for user answers. This describes the intended first version; no application code is implemented by this document.

## Purpose

A private, local-only scored feed of local hookup listings. It collects eligible listings from the configured sources, normalizes and deduplicates them, ranks them against the user's location and explicit preferences, and presents them for human review. The first version is a discovery and review tool, not an automated messaging or posting agent.

## Sources

| Source | Initial scope | Collection mode |
|---|---|---|
| Locanto | Local listings in the configured radius | Public listing pages where available |
| Doublelist | Local listings in the configured radius | Public listing pages where available |
| Squirt | Local listings in the configured radius | Explicitly configured public listings page; do not fetch member-only pages |
| Reddit (`reddit`) | R4R-tagged posts from `r/SLCr4r` and `r/r4r` | Public subreddit JSON; newest 100 per community |
| Grindr | Notifications explicitly made available through a user-authorized notification bridge | Passive notification ingestion only; no app actions |

A source may be marked unavailable when its response is blocked, missing, or requires a user-only credential or device setup. Reconnaissance on 2026-11-12 found HTTP 403 challenges from Locanto and Doublelist, a redirect followed by HTTP 404 from Squirt, and a redirect followed by HTTP 403 from Reddit. These are observations, not assumptions that the sources are permanently unavailable. The Grindr bridge is not configured yet.

## Collection and storage

- Use SQLite as the only database, at `db/hookups.sqlite3`.
- Store normalized listing fields: source, source ID or canonical URL, title, source text excerpt, posted/first-seen/last-seen timestamps, locality/distance when available, selected tags, score and score reasons, and review status.
- Deduplicate by source ID or canonical URL; if absent, use a normalized-content fingerprint within that source. Do not attempt to identify the same person across different services.
- Poll each enabled source on a 60-minute default schedule. The interval between starts for a source must never be less than 30 minutes, including manual refreshes and retries.
- On a source block, follow the project retry ladder: retry with backoff; retry with a Chrome user-agent and `Accept-Language`; use a headed persistent browser profile; apply stealth patches; wait out the challenge and reuse its cookies; use `PROXY_URL` from `.env`; then try a mobile page, RSS, or mirror. Cap work at 10 minutes per source per cycle, record unresolved gaps, and move on. The 30-minute minimum between source-poll starts always takes precedence over retries that would violate it.
- Record source health, status, and gaps. Do not treat a failed or blocked fetch as an empty feed.
- Retain listing records and source-derived text for 30 days from first seen, then delete them. Saved/bookmarked status does not extend this source-data retention period. Keep operational run metadata for the same 30-day period.
- Do not download or locally cache listing images in v1; show a source link instead.

## Scoring

Each eligible listing receives a deterministic score from 0 to 100. Apply hard exclusions and duplicate suppression before ranking. Default weighted inputs:

| Input | Weight | Default behavior |
|---|---:|---|
| Geographic fit | 40% | 100 at the selected center, decays to 0 at the configured radius; exclude listings known to be outside the radius. Missing location receives 0 for this component. |
| Recency | 25% | 100 if posted within 6 hours; decays linearly to 0 at 72 hours. Unknown timestamps receive 0. |
| Explicit preference fit | 20% | Match only user-entered preferred terms/tags; do not infer identity or preferences. |
| Intent clarity | 10% | Deterministic phrase/tag matches for clearly stated availability or intent; no model call required. |
| Listing completeness | 5% | Presence of useful fields such as body text, locality, and a usable source link. |

The weighted score is rounded to an integer. Show the score and the contributing reasons on each card. Sort by score descending, then newest posting time. User-entered excluded terms are hard filters. Do not score people using inferred sensitive traits. Listings explicitly indicating a person is under 18, coercion, or non-consent are suppressed rather than ranked.

## Digest

- Generate one local Markdown digest daily at 8:00 a.m. America/Denver, plus a matching view in the dashboard.
- Include up to 20 highest-ranked new or materially updated listings, grouped by source. Each item shows score, locality/distance if known, posted time, title, a short excerpt, matching reasons, and a link to the original listing.
- Keep digest files in `out/`; they contain source-derived content and follow the same 30-day retention period.
- A digest is a local draft, not a message. Do not email, text, post, or otherwise transmit it automatically.

## Dashboard pages

1. **Feed:** ranked cards, source and time filters, score explanations, and new/seen state.
2. **Listing detail:** normalized listing text, score breakdown, source timestamps, and original-source link.
3. **Review:** save, hide, or mark a listing as contacted. V1 does not send messages. If an outbound action is added later, show the exact recipient and message for explicit approval before each send.
4. **Digests:** current draft and retained daily digest archive.
5. **Sources:** enable/disable status, last attempt, next eligible poll time, result counts, errors, and recorded gaps.
6. **Preferences:** center and radius, preferred/excluded terms, scoring weights, and source toggles.

The dashboard is private and local-only by default; do not expose it on a public network or publish it as a public site.

## Hard rules

- SQLite only; no remote database.
- At least 30 minutes between starts for any source.
- Delete source-derived listing content after 30 days.
- Nothing sends, posts, swipes, or contacts anyone without the user's explicit approval of that exact action. V1 has no sender.
- Secrets live only in the uncommitted `.env` file. Never commit or log secret values. Do not put credentials in the database, source code, or reports.
- Grindr access is passive through the user-authorized notification bridge; the agent must not operate the Grindr app.
- Keep the feed/dashboard private; no public hosting or sharing by default.

## Open questions for the user

1. What city/ZIP or map point should define “local,” and is the default 25-mile radius acceptable?
2. Do you want any other local R4R subreddits in addition to `r/SLCr4r` and `r/r4r`?
3. Which device/platform should provide Grindr notifications, and what notification-bridge method do you want to use?
4. Which preferred and excluded terms/tags should seed ranking? Until specified, the preference-fit component is neutral/zero.
5. Is a daily 8:00 a.m. local Markdown digest plus dashboard view the right cadence and destination, or do you want a different schedule or delivery channel? Any delivery channel still requires approval before sending.
6. Is local-only dashboard access sufficient, or do you want a private remote-access design later?
