from datetime import datetime, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo

import src.scheduler as scheduler_module


def test_scheduler_registers_all_requested_jobs(tmp_path):
    database = tmp_path / "scheduler.sqlite3"
    scheduler = scheduler_module.build_scheduler(database, tmp_path / "digest.txt")
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        "source-locanto",
        "source-doublelist",
        "source-squirt",
        "source-reddit",
        "digest-morning",
        "digest-evening",
        "purge-03-local",
    }
    assert "±20%" in str(jobs["source-locanto"].trigger)
    assert "America/Denver" in str(jobs["digest-morning"].trigger)
    assert "08:00" in str(jobs["digest-morning"].trigger)
    assert "18:00" in str(jobs["digest-evening"].trigger)
    assert "03:00" in str(jobs["purge-03-local"].trigger)


def test_interval_jitter_is_symmetric(monkeypatch):
    previous = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    trigger = scheduler_module.SymmetricIntervalTrigger(30 * 60)
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda low, high: low)
    assert trigger.get_next_fire_time(previous, previous) == previous + timedelta(minutes=24)
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda low, high: high)
    assert trigger.get_next_fire_time(previous, previous) == previous + timedelta(minutes=36)


def test_cron_jitter_stays_within_twelve_minutes(monkeypatch):
    zone = ZoneInfo("America/Denver")
    now = datetime(2026, 11, 12, 17, 0, tzinfo=zone)
    trigger = scheduler_module.SymmetricCronTrigger(18, 0)
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda low, high: low)
    assert trigger.get_next_fire_time(None, now) == datetime(2026, 11, 12, 17, 48, tzinfo=zone)
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda low, high: high)
    assert trigger.get_next_fire_time(None, now) == datetime(2026, 11, 12, 18, 12, tzinfo=zone)


def test_source_floor_defers_early_jitter_and_runs_are_logged(tmp_path):
    database = tmp_path / "scheduler.sqlite3"
    scheduler_module.migrate(database)
    now = datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO runs (source, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
        (
            "locanto",
            (now - timedelta(minutes=29)).isoformat(timespec="seconds"),
            (now - timedelta(minutes=28, seconds=59)).isoformat(timespec="seconds"),
            "succeeded",
        ),
    )
    connection.commit()
    connection.close()
    allowed, reason = scheduler_module._source_floor_state(database, "locanto", now)
    assert not allowed
    assert reason == "source-floor-active:60s"
    status = scheduler_module.run_logged("purge", lambda: "purged=0", database, now)
    assert status == "succeeded"
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT source, status, error_summary FROM runs WHERE source = 'scheduler' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    connection.close()
    assert row == ("scheduler", "succeeded", "job=purge; purged=0")


def test_source_job_uses_runner_and_logs_registration(tmp_path):
    database = tmp_path / "source-job.sqlite3"
    now = datetime(2026, 11, 12, 13, tzinfo=timezone.utc)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return scheduler_module.subprocess.CompletedProcess(command, 0)

    status = scheduler_module.run_source_job("locanto", database, now, runner)
    assert status == "succeeded"
    assert len(calls) == 1
    assert calls[0][0][-1] == "src/sources/locanto.py"
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT source, status, error_summary FROM runs WHERE source = 'scheduler'"
    ).fetchone()
    connection.close()
    assert row == ("scheduler", "succeeded", "job=source:locanto; collector=complete")
