# Full self-audit — 2026-11-14

## Live
- The private dashboard is running at https://hookups-dashboard-demo-powerbottom.zo.computer. It uses a separate demo database containing ten synthetic listings; the live listings database was not changed by the endpoint audit.
- The scheduler is now a supervised, non-networked process service. All eight jobs registered: four source polls, two digests, nightly purge, and nightly backup.
- One existing backup in `~/backups` passes SQLite `integrity_check`; seven-file retention is configured.
- The public Grindr relay is https://powerbottom.zo.space/api/grindr-notifications. Health returned 200; an invalid bearer returned 401; the authenticated curl smoke test stored one sanitized synthetic row in a disposable database. Real Android Notification Access and Tasker fields remain unverified.
- Endpoint audit: all dashboard routes and webhook routes returned expected responses using a disposable database; public relay GET and unauthorized POST were also exercised.
- `pytest`: 60 passed, 1 Starlette deprecation warning.

## Degraded / not configured
- Reddit `--dry` returned 0 listings after an HTTP 403 challenge. `PROXY_URL` is unset; this is the second consecutive failure, so the circuit is not yet open. A proxy is needed to try the next rung; whether it must be residential is unverified.
- Locanto and Doublelist `--dry` made no request because their city/category settings are blank. Squirt made no request because its public listings URL is unset. These sources are idle, not currently failing.
- Grindr's server endpoint is healthy; actual Android notification access and Tasker event values remain unverified.

## Keys and profile values
- Needed for the next Reddit retry: `PROXY_URL` in `.env`.
- Needed for Telegram alerts and bot commands: `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`; both are blank.
- `MODEL_URL` is optional; deterministic local scoring remains active without it. `GRINDR_WEBHOOK_TOKEN` is configured; its value is not recorded here.
- Real source collection also needs explicit profile city/category values for Locanto and Doublelist, plus a public listings URL for Squirt.

## Highest-value next source
Add a Scruff Android notification bridge using the existing passive capture → authenticated webhook path. It should be validated on-device before production ingestion; this reuses the working bridge and avoids another challenge-prone scraper.
