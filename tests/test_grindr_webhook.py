from __future__ import annotations

import hashlib
import sqlite3

from fastapi.testclient import TestClient

import src.db as db
import src.grindr_webhook as bridge
from src.scheduler import source_health


TOKEN = "test-token-" + "x" * 40
ENDPOINT = "/api/grindr/notifications"


def _client(monkeypatch, tmp_path, token: str = TOKEN):
    database = tmp_path / "grindr.sqlite3"
    env_file = tmp_path / ".env"
    env_file.write_text(f"GRINDR_WEBHOOK_TOKEN={token}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(bridge, "ENV_PATH", env_file)
    monkeypatch.setattr(bridge, "DB_PATH", database)
    return TestClient(bridge.app), database


def test_notification_is_stripped_hashed_and_idempotent(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    payload = {
        "event_id": "tasker-event-123",
        "title": "Display Name",
        "body": "Text: 34, nearby. Reach me at alex@example.com, 555-123-4567, https://example.invalid/a, or @alex_name",
        "received_at": "ignored-device-time",
    }
    with client:
        first = client.post(ENDPOINT, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
        second = client.post(ENDPOINT, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    with sqlite3.connect(database) as connection:
        listings = connection.execute(
            "SELECT source, source_id, title, text_excerpt, canonical_url FROM listings"
        ).fetchall()
        runs = connection.execute("SELECT COUNT(*) FROM runs WHERE source = 'grindr'").fetchone()[0]
    assert len(listings) == 1
    source, source_id, title, body, canonical_url = listings[0]
    assert source == "grindr"
    assert source_id == hashlib.sha256(b"tasker-event-123").hexdigest()
    assert source_id != "tasker-event-123"
    assert title == "Grindr notification"
    assert canonical_url is None
    for sensitive in ("Display Name", "alex@example.com", "555-123-4567", "https://", "@alex_name"):
        assert sensitive not in body
    assert "[contact removed]" in body
    assert "[handle removed]" in body
    assert runs == 2
    assert source_health(database)["sources"]["grindr"]["last_run"]["status"] == "succeeded"


def test_auth_rejects_wrong_token_and_unconfigured_secret(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    payload = {"event_id": "1", "body": "hello"}
    with client:
        wrong = client.post(ENDPOINT, json=payload, headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401

    client, _ = _client(monkeypatch, tmp_path, token="")
    with client:
        unset = client.post(ENDPOINT, json=payload, headers={"Authorization": "Bearer unused"})
    assert unset.status_code == 503


def test_rejects_weak_token_oversized_and_invalid_payload(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, token="short")
    with client:
        weak = client.post(
            ENDPOINT,
            json={"event_id": "1", "body": "hello"},
            headers={"Authorization": "Bearer short"},
        )
    assert weak.status_code == 503

    client, _ = _client(monkeypatch, tmp_path)
    with client:
        oversized = client.post(
            ENDPOINT,
            content=b"x" * (bridge.MAX_BODY_BYTES + 1),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        invalid = client.post(
            ENDPOINT,
            content=b"{bad",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
    assert oversized.status_code == 413
    assert invalid.status_code == 400


def test_underage_notification_is_suppressed(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    with client:
        response = client.post(
            ENDPOINT,
            json={"event_id": "minor-event", "title": "Age 17", "body": "17 years old"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 202
    assert response.json()["suppressed"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_schema_v7_migration_preserves_rows_and_allows_grindr(tmp_path):
    database = tmp_path / "schema-v7.sqlite3"
    schema = db.SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "'grindr', 'grindr-via-notification-bridge'", "'grindr-via-notification-bridge'"
    )
    with sqlite3.connect(database) as connection:
        db._execute_schema(connection, schema)
        connection.execute(
            "INSERT INTO listings (id, source, dedupe_key, first_seen_at, last_seen_at) VALUES (1, 'reddit', 'kept', '2026-11-13T00:00:00+00:00', '2026-11-13T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO scores (listing_id, scored_at, score, geographic_points, recency_points, preference_points, intent_points, completeness_points) VALUES (1, '2026-11-13T00:00:00+00:00', 0, 0, 0, 0, 0, 0)"
        )
        connection.execute("INSERT INTO contacts (listing_id, contacted_at, created_at) VALUES (1, '2026-11-13T00:00:00+00:00', '2026-11-13T00:00:00+00:00')")
        connection.execute("INSERT INTO drafts (listing_id, body, created_at, updated_at) VALUES (1, 'preserve this', '2026-11-13T00:00:00+00:00', '2026-11-13T00:00:00+00:00')")
        connection.execute("PRAGMA user_version = 7")

    db.migrate(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("SELECT source, dedupe_key FROM listings WHERE id = 1").fetchone() == ("reddit", "kept")
        assert connection.execute("SELECT body FROM drafts WHERE listing_id = 1").fetchone() == ("preserve this",)
        assert connection.execute("INSERT INTO listings (source, dedupe_key, first_seen_at, last_seen_at) VALUES ('grindr', 'new', '2026-11-13T00:00:00+00:00', '2026-11-13T00:00:00+00:00')").rowcount == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
