from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import DEFAULT_DB_PATH, migrate
from src.digest import DEFAULT_OUTPUT
from src.notify import read_settings, render_live_digest

LOCAL_TIMEZONE = "America/Denver"
SOURCE_FLOOR_SECONDS = 30 * 60
SOURCE_TIMEOUT_SECONDS = 10 * 60
JITTER_FRACTION = 0.20
CRON_JITTER_SECONDS = 12 * 60
SOURCE_SCRIPTS = {
    "locanto": "src/sources/locanto.py",
    "doublelist": "src/sources/doublelist.py",
    "squirt": "src/sources/squirt.py",
    "reddit": "src/sources/reddit.py",
}


class SymmetricIntervalTrigger(BaseTrigger):
    def __init__(self, interval_seconds: float, jitter_fraction: float = JITTER_FRACTION):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if not 0 <= jitter_fraction < 1:
            raise ValueError("jitter_fraction must be between zero and one")
        self.interval_seconds = float(interval_seconds)
        self.jitter_fraction = float(jitter_fraction)

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime:
        base = now if previous_fire_time is None else previous_fire_time
        spread = self.interval_seconds * self.jitter_fraction
        offset = random.uniform(-spread, spread)
        next_fire = base + timedelta(seconds=self.interval_seconds + offset)
        return max(next_fire, now + timedelta(microseconds=1))

    def __str__(self) -> str:
        return f"interval[{self.interval_seconds}s ±{self.jitter_fraction:.0%}]"


class SymmetricCronTrigger(BaseTrigger):
    def __init__(self, hour: int, minute: int, timezone_name: str = LOCAL_TIMEZONE):
        self.hour = hour
        self.minute = minute
        self.timezone = ZoneInfo(timezone_name)
        self.jitter_seconds = CRON_JITTER_SECONDS
        self._cron = CronTrigger(hour=hour, minute=minute, timezone=self.timezone)

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime | None:
        reference = previous_fire_time
        if reference is not None:
            reference = reference + timedelta(seconds=self.jitter_seconds)
        base = self._cron.get_next_fire_time(reference, now)
        if base is None:
            return None
        earliest_offset = max(-self.jitter_seconds, (now - base).total_seconds() + 0.001)
        offset = random.uniform(earliest_offset, self.jitter_seconds)
        return base + timedelta(seconds=offset)

    def __str__(self) -> str:
        return f"cron[{self.hour:02d}:{self.minute:02d} {self.timezone.key} ±12m]"


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _log_start(db_path: str | Path, job_name: str, now: datetime) -> int:
    migrate(db_path)
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        cursor = connection.execute(
            "INSERT INTO runs (source, started_at, status, error_summary) VALUES ('scheduler', ?, 'running', ?)",
            (now.isoformat(timespec="seconds"), f"job={job_name}"),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _log_finish(
    db_path: str | Path,
    run_id: int,
    job_name: str,
    status: str,
    summary: str,
    finished_at: datetime,
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error_summary = ? WHERE id = ? AND source = 'scheduler'",
            (finished_at.isoformat(timespec="seconds"), status, f"job={job_name}; {summary}"[:1000], run_id),
        )
        connection.commit()
    finally:
        connection.close()


def run_logged(
    job_name: str,
    action: Callable[[], str],
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> str:
    started = _utc(now)
    run_id = _log_start(db_path, job_name, started)
    status = "succeeded"
    summary = "complete"
    try:
        summary = action() or "complete"
    except Exception as error:
        status = "failed"
        summary = f"error={type(error).__name__}"
    finished = _utc(now) if now is not None else _utc()
    _log_finish(db_path, run_id, job_name, status, summary, finished)
    logging.info("job=%s status=%s %s", job_name, status, summary)
    return status


def _source_floor_state(db_path: str | Path, source: str, now: datetime) -> tuple[bool, str]:
    migrate(db_path)
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        row = connection.execute(
            "SELECT id, started_at, status FROM runs WHERE source = ? ORDER BY started_at DESC, id DESC LIMIT 1",
            (source,),
        ).fetchone()
        if not row:
            return True, "due"
        started = _timestamp(row[1])
        if started is None:
            return False, "invalid-source-start-time"
        elapsed = (now - started).total_seconds()
        if row[2] == "running" and elapsed <= SOURCE_TIMEOUT_SECONDS:
            return False, "source-run-in-progress"
        if row[2] == "running":
            connection.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error_summary = 'scheduler marked stale run after ten-minute cap' WHERE id = ?",
                (now.isoformat(timespec="seconds"), row[0]),
            )
            connection.commit()
        remaining = SOURCE_FLOOR_SECONDS - elapsed
        if remaining > 0:
            return False, f"source-floor-active:{int(remaining)}s"
        return True, "due"
    finally:
        connection.close()


def run_source_job(
    source: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    if source not in SOURCE_SCRIPTS:
        raise ValueError(f"unsupported source: {source}")
    current = _utc(now)

    def action() -> str:
        allowed, reason = _source_floor_state(db_path, source, current)
        if not allowed:
            return f"skipped={reason}"
        command = [sys.executable, "-u", SOURCE_SCRIPTS[source]]
        execute = runner or subprocess.run
        try:
            result = execute(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SOURCE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("collector exceeded ten-minute cap") from None
        if result.returncode != 0:
            raise RuntimeError(f"collector exited with code {result.returncode}")
        return "collector=complete"

    return run_logged(f"source:{source}", action, db_path, current)


def run_digest_job(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_OUTPUT,
    now: datetime | None = None,
) -> str:
    def action() -> str:
        settings = read_settings()
        render_live_digest(db_path, output_path, model_url=settings["model_url"], now=now, write=True)
        return "digest=rendered; delivery=manual-only"

    return run_logged("digest", action, db_path, now)


def purge_expired(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_OUTPUT,
    now: datetime | None = None,
) -> dict[str, int]:
    current = _utc(now)
    listing_cutoff = (current - timedelta(days=30)).isoformat(timespec="seconds")
    cache_cutoff = (current - timedelta(minutes=30)).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        listing_deleted = connection.execute(
            "DELETE FROM listings WHERE last_seen_at < ?", (listing_cutoff,)
        ).rowcount
        cache_deleted = connection.execute(
            "DELETE FROM http_cache WHERE fetched_at < ?", (cache_cutoff,)
        ).rowcount
        score_cache_deleted = connection.execute(
            "DELETE FROM score_cache WHERE expires_at <= ?", (current.isoformat(timespec="seconds"),)
        ).rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    digest_deleted = 0
    digest = Path(output_path)
    if digest.is_file() and digest.stat().st_mtime < current.timestamp() - 30 * 86400:
        digest.unlink()
        digest_deleted = 1
    return {
        "listings": max(0, listing_deleted),
        "http_cache": max(0, cache_deleted),
        "score_cache": max(0, score_cache_deleted),
        "digest_files": digest_deleted,
    }


def run_purge_job(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_OUTPUT,
    now: datetime | None = None,
) -> str:
    def action() -> str:
        result = purge_expired(db_path, output_path, now)
        return "; ".join(f"purged_{key}={value}" for key, value in result.items())

    return run_logged("purge", action, db_path, now)


def build_scheduler(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_OUTPUT,
    timezone_name: str = LOCAL_TIMEZONE,
) -> BackgroundScheduler:
    zone = ZoneInfo(timezone_name)
    migrate(db_path)
    scheduler = BackgroundScheduler(
        timezone=zone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    interval_jobs = (("locanto", 30), ("doublelist", 45), ("squirt", 60), ("reddit", 60))
    for source, minutes in interval_jobs:
        scheduler.add_job(
            run_source_job,
            trigger=SymmetricIntervalTrigger(minutes * 60),
            id=f"source-{source}",
            name=f"source:{source}",
            args=(source, str(db_path)),
        )
    scheduler.add_job(
        run_digest_job,
        trigger=SymmetricCronTrigger(8, 0, timezone_name),
        id="digest-morning",
        name="digest:08:00-local",
        args=(str(db_path), str(output_path)),
    )
    scheduler.add_job(
        run_digest_job,
        trigger=SymmetricCronTrigger(18, 0, timezone_name),
        id="digest-evening",
        name="digest:18:00-local",
        args=(str(db_path), str(output_path)),
    )
    scheduler.add_job(
        run_purge_job,
        trigger=SymmetricCronTrigger(3, 0, timezone_name),
        id="purge-03-local",
        name="purge:03:00-local",
        args=(str(db_path), str(output_path)),
    )
    return scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timezone", default=LOCAL_TIMEZONE)
    parser.add_argument("--check-seconds", type=int, default=0)
    parser.add_argument("--list-jobs", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scheduler = build_scheduler(args.database, args.output, args.timezone)
    if args.list_jobs:
        for job in scheduler.get_jobs():
            print(f"{job.id}\t{job.trigger}")
        return 0
    scheduler.start()
    print(f"Scheduler started in {args.timezone}; registered {len(scheduler.get_jobs())} jobs.", flush=True)
    for job in scheduler.get_jobs():
        print(f"{job.id}\t{job.trigger}", flush=True)
    if args.check_seconds > 0:
        try:
            time.sleep(args.check_seconds)
        finally:
            scheduler.shutdown(wait=True)
        print(f"Registration check passed after {args.check_seconds}s.", flush=True)
        return 0
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown(wait=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())