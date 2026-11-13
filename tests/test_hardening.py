from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import src.scheduler as scheduler


START = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_circuit_opens_after_three_failures_alerts_once_and_recovers(tmp_path, monkeypatch):
    database = tmp_path / "circuit.sqlite3"
    calls = []
    alerts = []

    def runner(command, **kwargs):
        calls.append(command)
        return scheduler.subprocess.CompletedProcess(command, 1 if len(calls) <= 3 else 0)

    monkeypatch.setattr(scheduler, "read_settings", lambda: {"token": "token", "chat_id": "123"})
    monkeypatch.setattr(scheduler, "send_digest", lambda token, chat_id, text: alerts.append(text) or 1)
    for minute in (0, 31, 62):
        result = scheduler.run_source_job("locanto", database, START + timedelta(minutes=minute), runner)
        assert result == "failed"
    assert len(alerts) == 1
    assert "3 consecutive polls" in alerts[0]
    health = scheduler.source_health(database, START + timedelta(minutes=62))
    assert health["sources"]["locanto"]["consecutive_failures"] == 3
    assert health["sources"]["locanto"]["error_count_30d"] == 3
    assert health["sources"]["locanto"]["circuit_open"] is True
    skipped = scheduler.run_source_job("locanto", database, START + timedelta(minutes=93), runner)
    assert skipped == "succeeded"
    assert len(calls) == 3
    recovered = scheduler.run_source_job("locanto", database, START + timedelta(minutes=123), runner)
    assert recovered == "succeeded"
    assert len(calls) == 4
    assert scheduler.consecutive_failures(database, "locanto")[0] == 0
    assert len(alerts) == 1


def test_nightly_sqlite_backups_are_private_valid_and_retained_seven(tmp_path):
    database = tmp_path / "source.sqlite3"
    backup_dir = tmp_path / "backups"
    scheduler.migrate(database)
    for day in range(9):
        result = scheduler.backup_database(
            database,
            backup_dir,
            now=START + timedelta(days=day),
        )
        backup = result["path"]
        with sqlite3.connect(backup) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backups = sorted(backup_dir.glob("hookups-*.sqlite3"))
    assert len(backups) == 7
    assert len(list(backup_dir.glob("*.tmp"))) == 0
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)
