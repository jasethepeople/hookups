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

Each eligible listing receives a deterministic 0–100 score. Apply hard exclusions and duplicate suppression first. The scorer uses these fixed weights:

| Input | Weight | Default behavior |
|---|---:|---|
| Distance | 25 | Linear decay from 25 at zero distance to 0 at `max_drive_minutes × 0.5` miles (30 mph estimate); missing distance/profile maximum earns 0. |
| Last seen | 20 | Linear decay to 0 over 72 hours; missing or invalid timestamps earn 0. |
| Age | 15 | Full points only when the explicit age lies within the inclusive configured adult age range; missing/out-of-range age earns 0. Under-18 listings are suppressed. |
| Body type / position | 15 | Split evenly across explicitly configured body-type and position terms; only explicit listing matches count. |
| Hosting | 10 | Awarded only for an explicit match between the configured host/travel preference and listing text or tags. |
| Photos | 10 | Awarded when at least one source photo URL is present; images are not downloaded. |
| Discretion | 5 | Awarded only when an explicit configured discretion term appears in listing text. |

Round each component and the total to the nearest integer. A configured dealbreaker phrase in the title, body, tags, or location hard-zeros the total. Superseded duplicates receive 0. `MODEL_URL` may explain the component-based result in two sentences but cannot change the deterministic score; only the component values are sent to it. Cache results by record ID and input hash for up to 30 days. If the model URL is unset or unavailable, use the deterministic two-sentence explanation. Do not score inferred sensitive traits, and suppress records explicitly indicating age under 18, coercion, or non-consent.

## Digest

- Render a local plain-text digest to `out/digest.txt`; include the top 8 total listings with scores at or above 50, grouped alphabetically by source. Show title, score, distance, age, the two-sentence rationale, and original URL.
- Add a `New since last digest` count based on eligible records whose `last_seen_at` is later than the previous digest’s `Generated` timestamp. On the first render, count all eligible records with a `last_seen_at`.
- The suppressed count includes normalization suppressions, hard-zero dealbreakers, and dedupe losers. Listings below threshold and entries beyond the top-8 cap are not counted as suppressed.
- Keep digest files in `out/`; they contain source-derived content and follow the same 30-day retention period.
- A digest is a local draft, not a message. Never transmit automatically; Telegram delivery occurs only after an authorized `/hookups` command from the configured chat.

## Dashboard pages

1. **Feed:** cards sorted by score with source and minimum-score filters; show score, distance, age, explanation, and source link.
2. **Listing detail:** normalized listing text, score breakdown, source timestamps, and original-source link.
3. **Review:** mark seen, hide, or block a source domain; copy only a user-provided opener. V1 does not send messages. If an outbound action is added later, show the exact recipient and message for explicit approval before each send.
4. **Digests:** current draft and retained daily digest archive.
5. **Sources:** enable/disable status, last attempt, next eligible poll time, result counts, errors, and recorded gaps.
6. **Preferences:** center and radius, preferred/excluded terms, scoring weights, and source toggles.

The dashboard is private and local-only by default; when hosted on Zo, run it as a private HTTP service. Demo rows live in a separate ignored SQLite database and never enter the live listings database. Do not expose the dashboard on a public network or publish it as a public site.

## Drafts

- Generate two unsent variants for each of the three highest-scored visible, non-superseded eligible listings; persist drafts idempotently.
- Grindr variants are stats-forward and at most 10 words; Doublelist variants are 3–4 sentences containing explicit self-stats plus one specific title/body hook; Reddit variants are short comments.
- Use only facts in `profile.self_stats`; never infer personal facts. Skip Grindr and Doublelist until self-stats are configured. Drafting never sends a message.- On the dashboard, approval copies the exact variant to the clipboard and records `contacts.status='sent'` with its UTC `ts`; clipboard failure prevents approval. Editing and skipping update the local draft only. The private Telegram bot accepts `/log <url> <replied|ghosted|met>` for a known listing; it never contacts the listing author.

## Scheduler

- Run Locanto every 30 minutes, Doublelist every 45 minutes, and Squirt and Reddit every 60 minutes.
- Apply symmetric ±20% interval jitter; scheduled clock-time jobs use a ±12-minute window around their local anchor.
- Keep the minimum 30-minute source-start floor; defer early jittered source runs until eligible.
- Render local digests at 08:00 and 18:00 America/Denver time, purge expired data at 03:00, and log every scheduler job in `runs`.
- Scheduled digests are local drafts. Telegram delivery is only triggered by a private `/hookups` command.

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
5. Is local-only dashboard access sufficient, or do you want a private remote-access design later?
