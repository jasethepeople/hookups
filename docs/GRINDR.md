# Grindr notification bridge

The bridge is passive: it receives notifications that Android has already shown, stores a minimal sanitized preview, and never opens or acts in Grindr. New records use `source="grindr"` and a one-way SHA-256 hash of the sender-generated event ID as their source ID. The notification title is discarded; the body is stripped of email addresses, phone numbers, URLs, and `@handles`, then capped at 400 characters. The raw payload and event ID are not stored. Explicit under-18 or coercion/non-consent notifications are suppressed.

## Endpoint

- Public relay: `https://powerbottom.zo.space/api/grindr-notifications`
- Health check: `GET https://powerbottom.zo.space/api/grindr-notifications` (no token; returns only `{"ok":true}`)
- Ingest: `POST https://powerbottom.zo.space/api/grindr-notifications`
- Authentication: `X-Grindr-Token: <GRINDR_WEBHOOK_TOKEN>` (the relay converts this to a Bearer header for the private receiver)
- Accepted body types: JSON or `application/x-www-form-urlencoded`; maximum body size is 8 KiB.

The Zo Space route is public so Android can reach it, but ingest requires the 43-character bearer secret stored as `GRINDR_WEBHOOK_TOKEN` in `~/hookups/.env`. The relay forwards requests only to the local private dashboard service; the dashboard itself remains private. Keep `.env` mode `600`, never put the token in this file or a commit, and rotate it if it is exposed. The phone-side Tasker sender necessarily needs a copy of the token; do not export or share its Tasker project.

## Disposable curl smoke test

Run from `~/hookups`:

```bash
scripts/test_grindr_webhook.sh
```

This starts a temporary local receiver, reads the token from `.env` into a mode-`600` temporary curl config, posts a synthetic notification, asserts the sanitized listing and run record, then removes the temporary database and token config. It does not write test listings to the live database.

For a one-off request to the live endpoint, replace the payload with a notification you intend to retain. Avoid echoing the token or putting it directly in shell history. A missing or incorrect token returns `401`; an unset or weak token returns `503`.

```bash
TOKEN="$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("GRINDR_WEBHOOK_TOKEN", ""))')"
curl --fail-with-body --silent --show-error \
  -H "X-Grindr-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary '{"event_id":"replace-with-unique-id","title":"","body":"notification preview"}' \
  https://powerbottom.zo.space/api/grindr-notifications
unset TOKEN
```

A successful new notification returns `200`; a repeated event ID updates the existing row and returns `200` with `duplicate: true`; a suppressed underage/non-consensual notification returns `202` and creates no listing. The server timestamps receipt; `received_at` from the device is not trusted.

## Tasker setup (Android)

1. Install Tasker, then grant it Android's **Notification access** permission. In Tasker, create a Profile using **Event → UI → Notification** and set **Owner Application** to Grindr. Do not add launch, tap, swipe, or reply actions.
2. Attach a Task. Set three local variables:
   - `%event_id` = `%TIMEMS`
   - `%event_title` = `%evtprm2`
   - `%event_body` = `%evtprm3`
3. Add **JavaScriptlet** with this code. Tasker exposes lower-case task variables directly to JavaScriptlet code, and the `payload` output becomes `%payload` for the next action:

   ```javascript
   var payload = JSON.stringify({
     event_id: String(event_id),
     title: event_title || "",
     body: event_body || "",
     received_at: new Date().toISOString()
   });
   ```

4. Add **Net → HTTP Request**:
   - Method: `POST`
   - URL: `https://powerbottom.zo.space/api/grindr-notifications`
   - Headers:
     ```text
     X-Grindr-Token:%GrindrToken
     Content-Type:application/json
     ```
   - Body: `%payload`
   - Timeout: 30 seconds
5. Create the Tasker global variable `%GrindrToken` on the phone using the value of `GRINDR_WEBHOOK_TOKEN` from `~/hookups/.env`. Keep the Tasker project local; an exported Tasker profile may contain this secret. The server's `.env` remains the canonical copy. Do not post the value into chat or this documentation.
6. Test with one notification while watching Tasker's HTTP response code `%HTTPR`. `200` means stored/updated, `202` means deliberately suppressed, `401` means the token is wrong, and `503` means the server token is missing/too short. Tasker's notification preview settings may hide the body; only enable a more detailed preview if you are comfortable with that content appearing on the phone's lock screen.

Tasker's notification-event documentation uses `%evtprm2` for the title and `%evtprm3` (and following parameters) for other notification fields. Confirm the variables with a harmless test notification on the actual Android version before enabling the Profile: [Tasker variables](https://tasker.joaoapps.com/userguide/en/variables.html), [Tasker event context](https://tasker.joaoapps.com/userguide/en/eventcontext.html), [Tasker HTTP Request](https://tasker.joaoapps.com/userguide/en/help/ah_http_request.html), [Tasker JavaScript support](https://tasker.joaoapps.com/userguide/en/javascript.html).

## What is retained

Only the fixed title `Grindr notification`, the sanitized preview, parsed locality when present, receipt timestamps, the hashed source ID, and normal listing/run metadata are stored. The dashboard, score, dedupe, and 30-day expiry rules apply. Notification captures are event-driven and do not poll Grindr or change the 30-minute source-poll schedule.
