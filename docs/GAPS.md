# Gaps

- 2026-11-12 recon: direct HTTP HEAD requests to Locanto and Doublelist returned HTTP 403 with `cf-mitigated: challenge`; retrying after backoff with a Chrome user-agent and `Accept-Language` did not change the result. Reddit redirected to `www.reddit.com`, which returned HTTP 403. Squirt redirected to `www.squirt.org`, which returned HTTP 404. The headed-browser/proxy/fallback-source ladder was not needed to report the requested HTTP headers; revisit if full-page collection is later required.
