#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
chmod 700 "$TMP"
PORT="$(.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
DB="$TMP/grindr-test.sqlite3"
CURL_CONFIG="$TMP/curl.conf"
SERVER_PID=""
cleanup() {
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT
.venv/bin/python - "$ROOT/.env" "$CURL_CONFIG" <<'PY'
import os
import sys
from dotenv import dotenv_values

token = str(dotenv_values(sys.argv[1]).get("GRINDR_WEBHOOK_TOKEN") or "")
if len(token) < 32:
    raise SystemExit("GRINDR_WEBHOOK_TOKEN must contain at least 32 characters")
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    output.write(f'header = "Authorization: Bearer {token}"\n')
PY
GRINDR_DB_PATH="$DB" .venv/bin/uvicorn src.grindr_webhook:app --host 127.0.0.1 --port "$PORT" >"$TMP/server.log" 2>&1 &
SERVER_PID=$!
BASE_URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 40); do
    if curl --silent --fail "$BASE_URL/health" >/dev/null; then
        break
    fi
    sleep 0.25
done
curl --silent --fail "$BASE_URL/health" >/dev/null
EVENT_ID="curl-test-$(date +%s%N)"
PAYLOAD="$(.venv/bin/python - "$EVENT_ID" <<'PY'
import json
import sys
print(json.dumps({
    "event_id": sys.argv[1],
    "title": "Synthetic test sender",
    "body": "Age 35; nearby. Contact test.user@example.invalid or 555-123-4567 @synthetic_test",
    "received_at": "test-only",
}))
PY
)"
RESPONSE="$(curl --silent --show-error --fail-with-body --config "$CURL_CONFIG" -H 'Content-Type: application/json' --data-binary "$PAYLOAD" "$BASE_URL/api/grindr/notifications")"
printf '%s\n' "$RESPONSE"
printf '%s' "$RESPONSE" | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); assert data["ok"] and not data["duplicate"] and not data["suppressed"]'
.venv/bin/python - "$DB" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    row = connection.execute("SELECT source, title, text_excerpt, canonical_url FROM listings").fetchone()
    run_count = connection.execute("SELECT COUNT(*) FROM runs WHERE source = 'grindr'").fetchone()[0]
assert row is not None and row[0] == "grindr" and row[1] == "Grindr notification" and row[3] is None
assert "test.user@example.invalid" not in row[2] and "555-123-4567" not in row[2] and "@synthetic_test" not in row[2]
assert run_count == 1
print("curl test passed; one sanitized grindr listing and one run recorded in a disposable database")
PY
