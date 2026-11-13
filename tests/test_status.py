from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import src.scheduler as scheduler
import src.web as web


def test_status_shows_last_run_and_30_day_source_errors(monkeypatch, tmp_path):
    database = tmp_path / "status.sqlite3"
    scheduler.migrate(database)
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO runs (source, started_at, finished_at, status, error_summary) VALUES (?, ?, ?, ?, ?)",
            ("locanto", (now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).isoformat(), "failed", "http_503"),
        )
        connection.execute(
            "INSERT INTO runs (source, started_at, finished_at, status, error_summary) VALUES (?, ?, ?, ?, NULL)",
            ("locanto", (now - timedelta(hours=1)).isoformat(), (now - timedelta(hours=1)).isoformat(), "succeeded"),
        )
    monkeypatch.setattr(web, "DB_PATH", database)
    monkeypatch.setattr(web, "STATUS_DB_PATH", database)
    monkeypatch.setattr(web, "DEMO_MODE", False)
    with TestClient(web.app) as client:
        result = client.get("/status")
    assert result.status_code == 200
    source = result.json()["sources"]["locanto"]
    assert source["last_run"]["status"] == "succeeded"
    assert source["error_count_30d"] == 1
    assert source["consecutive_failures"] == 0
    assert source["circuit_open"] is False
