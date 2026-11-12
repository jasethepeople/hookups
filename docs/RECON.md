# Machine reconnaissance

Captured 2026-11-12 20:15 UTC (container clock). No packages were installed and no application code was written.

## Local environment

| Check | Result |
|---|---|
| Python | Python 3.12.1 |
| Node.js | v22.23.5 |
| Git | git version 2.39.5 |
| Project virtualenv | Present at `.venv`; requested packages all import successfully: httpx, selectolax, playwright, apscheduler, python-dotenv, rich, jinja2, fastapi, uvicorn |
| Playwright | 1.65.1 |
| Chromium | Present at `/root/.cache/ms-playwright/chromium-1254/chrome-linux64/chrome`; headless launch and test page succeeded |
| Free disk | 510G available of 512G on `/` (1% used) |
| curl | 7.88.1 |

## HTTP header checks

Initial requests were `curl -I --max-time 30 https://<host>/` (no redirect following). Retry requests were made after a 2-second backoff with a Chrome user-agent, `Accept-Language: en-US,en;q=0.9`, and `-L`. `Set-Cookie` values are redacted; other response headers are retained. Retry status: Locanto 403 Cloudflare challenge; Doublelist 403 Cloudflare challenge; Squirt redirects to `www.squirt.org` and returns 404; Reddit redirects to `www.reddit.com` and returns 403.

### Initial curl -I

#### locanto.com

```http
HTTP/2 403 
date: Thu, 12 Nov 2026 20:13:23 GMT
content-type: text/html; charset=UTF-8
content-length: 12021
accept-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cf-mitigated: challenge
x-frame-options: SAMEORIGIN
server: cloudflare
critical-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cross-origin-embedder-policy: require-corp
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
origin-agent-cluster: ?1
permissions-policy: accelerometer=(),camera=(),clipboard-read=(),clipboard-write=(),geolocation=(),gyroscope=(),hid=(),magnetometer=(),microphone=(),payment=(),publickey-credentials-get=(),screen-wake-lock=(),serial=(),sync-xhr=(),usb=(),xr-spatial-tracking=*
referrer-policy: same-origin
server-timing: chlray;desc="a598326fbcb7abe9"
x-content-type-options: nosniff
set-cookie: [redacted]
cf-ray: a598326fbcb7abe9-IAD

```

#### doublelist.com

```http
HTTP/2 403 
date: Thu, 12 Nov 2026 20:13:23 GMT
content-type: text/html; charset=UTF-8
content-length: 5330
accept-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cf-mitigated: challenge
content-security-policy: default-src 'none'; script-src 'nonce-afSwcHqM6ILq5JJ6qYsHBI' 'unsafe-eval' https://challenges.cloudflare.com; script-src-attr 'none'; style-src 'unsafe-inline'; img-src 'self' https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; frame-src 'self' https://challenges.cloudflare.com blob:; child-src 'self' https://challenges.cloudflare.com blob:; worker-src blob:; form-action http: https:; base-uri 'self'
server: cloudflare
critical-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cross-origin-embedder-policy: require-corp
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
origin-agent-cluster: ?1
permissions-policy: accelerometer=(),camera=(),clipboard-read=(),clipboard-write=(),geolocation=(),gyroscope=(),hid=(),magnetometer=(),microphone=(),payment=(),publickey-credentials-get=(),screen-wake-lock=(),serial=(),sync-xhr=(),usb=(),xr-spatial-tracking=*
referrer-policy: same-origin
server-timing: chlray;desc="a598326facd46d4d"
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
strict-transport-security: max-age=0; includeSubDomains; preload
cf-ray: a598326facd46d4d-IAD
alt-svc: h3=":443"; ma=86400

```

#### squirt.org

```http
HTTP/2 301 
location: https://www.squirt.org/
server: BIGIP
content-length: 0

```

#### reddit.com

```http
HTTP/2 301 
retry-after: 0
location: https://www.reddit.com/
accept-ranges: bytes
date: Thu, 12 Nov 2026 20:13:23 GMT
via: 1.1 varnish
server-timing: reddit-ct;desc="dn=FT,p=IAD,cs=HIT"
strict-transport-security: max-age=31536000; includeSubdomains; preload
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
x-xss-protection: 1; mode=block
cache-control: private, max-age=3600
server: snooserv
report-to: {"group": "w3-reporting-nel", "max_age": 14400, "include_subdomains": true,  "endpoints": [{ "url": "https://w3-reporting-nel.reddit.com/reports" }]}, {"group": "w3-reporting", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting.reddit.com/reports" }]}, {"group": "w3-reporting-csp", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting-csp.reddit.com/reports" }]}
nel: {"report_to": "w3-reporting-nel", "max_age": 14400, "include_subdomains": false, "success_fraction": 1.0, "failure_fraction": 1.0}
content-length: 0

```

### Retry: curl -I -L with Chrome UA and Accept-Language

#### locanto.com

```http
HTTP/2 403 
date: Thu, 12 Nov 2026 20:14:12 GMT
content-type: text/html; charset=UTF-8
content-length: 12277
accept-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cf-mitigated: challenge
x-frame-options: SAMEORIGIN
server: cloudflare
critical-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cross-origin-embedder-policy: require-corp
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
origin-agent-cluster: ?1
permissions-policy: accelerometer=(),camera=(),clipboard-read=(),clipboard-write=(),geolocation=(),gyroscope=(),hid=(),magnetometer=(),microphone=(),payment=(),publickey-credentials-get=(),screen-wake-lock=(),serial=(),sync-xhr=(),usb=(),xr-spatial-tracking=*
referrer-policy: same-origin
server-timing: chlray;desc="a59833a07e15d471"
x-content-type-options: nosniff
set-cookie: [redacted]
cf-ray: a59833a07e15d471-IAD

```

#### doublelist.com

```http
HTTP/2 403 
date: Thu, 12 Nov 2026 20:14:12 GMT
content-type: text/html; charset=UTF-8
content-length: 5565
accept-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cf-mitigated: challenge
content-security-policy: default-src 'none'; script-src 'nonce-lDWVGL61BJ32MuFqqZc228' 'unsafe-eval' https://challenges.cloudflare.com; script-src-attr 'none'; style-src 'unsafe-inline'; img-src 'self' https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; frame-src 'self' https://challenges.cloudflare.com blob:; child-src 'self' https://challenges.cloudflare.com blob:; worker-src blob:; form-action http: https:; base-uri 'self'
server: cloudflare
critical-ch: Sec-CH-UA-Bitness, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version, Sec-CH-UA-Mobile, Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform, Sec-CH-UA, UA-Bitness, UA-Arch, UA-Full-Version, UA-Mobile, UA-Model, UA-Platform-Version, UA-Platform, UA
cross-origin-embedder-policy: require-corp
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
origin-agent-cluster: ?1
permissions-policy: accelerometer=(),camera=(),clipboard-read=(),clipboard-write=(),geolocation=(),gyroscope=(),hid=(),magnetometer=(),microphone=(),payment=(),publickey-credentials-get=(),screen-wake-lock=(),serial=(),sync-xhr=(),usb=(),xr-spatial-tracking=*
referrer-policy: same-origin
server-timing: chlray;desc="a59833a07f682552"
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
strict-transport-security: max-age=0; includeSubDomains; preload
cf-ray: a59833a07f682552-IAD
alt-svc: h3=":443"; ma=86400

```

#### squirt.org

```http
HTTP/2 301 
location: https://www.squirt.org/
server: BIGIP
content-length: 0

HTTP/2 404 
cache-control: private
content-length: 3284
content-type: text/html; charset=utf-8
x-ua-compatible: IE=10
date: Thu, 12 Nov 2026 20:14:12 GMT
set-cookie: [redacted]
x-frame-options: SAMEORIGIN
x-server: P-SQRT-D-WEB7
content-security-policy: frame-ancestors 'self'
x-content-security-policy: frame-ancestors 'self'
set-cookie: [redacted]
set-cookie: [redacted]

```

#### reddit.com

```http
HTTP/2 301 
retry-after: 0
location: https://www.reddit.com/
accept-ranges: bytes
date: Thu, 12 Nov 2026 20:14:12 GMT
via: 1.1 varnish
server-timing: reddit-ct;desc="dn=FT,p=IAD,cs=HIT"
strict-transport-security: max-age=31536000; includeSubdomains; preload
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
x-xss-protection: 1; mode=block
cache-control: private, max-age=3600
server: snooserv
report-to: {"group": "w3-reporting-nel", "max_age": 14400, "include_subdomains": true,  "endpoints": [{ "url": "https://w3-reporting-nel.reddit.com/reports" }]}, {"group": "w3-reporting", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting.reddit.com/reports" }]}, {"group": "w3-reporting-csp", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting-csp.reddit.com/reports" }]}
nel: {"report_to": "w3-reporting-nel", "max_age": 14400, "include_subdomains": false, "success_fraction": 1.0, "failure_fraction": 1.0}
content-length: 0

HTTP/2 403 
retry-after: 0
content-type: text/html
cache-control: private, no-store
accept-ranges: bytes
date: Thu, 12 Nov 2026 20:14:12 GMT
via: 1.1 varnish
server-timing: reddit-ct;desc="dn=FT,p=IAD,cs=MISS"
strict-transport-security: max-age=31536000; includeSubdomains
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
x-xss-protection: 1; mode=block
set-cookie: [redacted]
server: snooserv
report-to: {"group": "w3-reporting-nel", "max_age": 14400, "include_subdomains": true,  "endpoints": [{ "url": "https://w3-reporting-nel.reddit.com/reports" }]}, {"group": "w3-reporting", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting.reddit.com/reports" }]}, {"group": "w3-reporting-csp", "max_age": 14400, "include_subdomains": true, "endpoints": [{ "url": "https://w3-reporting-csp.reddit.com/reports" }]}
nel: {"report_to": "w3-reporting-nel", "max_age": 14400, "include_subdomains": false, "success_fraction": 1.0, "failure_fraction": 1.0}
content-length: 190240

```
