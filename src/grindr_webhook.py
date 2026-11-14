from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.db import DEFAULT_DB_PATH, migrate
from src.normalize import _is_suppressed, extract_age, extract_location


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
DB_PATH = Path(os.environ.get("GRINDR_DB_PATH", str(DEFAULT_DB_PATH)))
MAX_BODY_BYTES = 8192
PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d[\d .()\-]{7,}\d(?!\w)")
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
HANDLE_PATTERN = re.compile(r"(?<!\w)@[A-Za-z0-9_.-]{2,64}")


class Notification(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=240)
    body: str = Field(default="", max_length=2000)
    received_at: str | None = Field(default=None, max_length=64)


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strip_text(value: str, limit: int) -> str:
    text = unescape(value)
    text = re.sub(r"<[^>]*>", " ", text)
    text = URL_PATTERN.sub("[link removed]", text)
    text = EMAIL_PATTERN.sub("[contact removed]", text)
    text = PHONE_PATTERN.sub("[contact removed]", text)
    text = HANDLE_PATTERN.sub("[handle removed]", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[^:]{1,64}:\s*", "", text)
    return text[:limit]


def _read_payload(request: Request, body: bytes) -> Notification:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    try:
        if content_type == "application/json":
            payload = json.loads(body)
        elif content_type == "application/x-www-form-urlencoded":
            values = parse_qs(body.decode("utf-8", "strict"), keep_blank_values=True, max_num_fields=12)
            payload = {key: value[-1] for key, value in values.items()}
        else:
            raise HTTPException(415, "Use application/json or application/x-www-form-urlencoded")
        return Notification.model_validate(payload)
    except HTTPException:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as error:
        raise HTTPException(400, "Invalid notification payload") from error


def _authorized(request: Request) -> bool:
    token = str(dotenv_values(ENV_PATH).get("GRINDR_WEBHOOK_TOKEN") or "")
    supplied = request.headers.get("authorization", "")
    if len(token) < 32:
        raise HTTPException(503, "Webhook token must be configured with at least 32 characters")
    if not supplied.startswith("Bearer "):
        return False
    candidate = supplied[7:]
    return hmac.compare_digest(candidate, token)


def _record_notification(notification: Notification, db_path: str | Path, now: str | None = None) -> dict:
    event_id = notification.event_id.strip()
    if not event_id:
        raise HTTPException(400, "event_id is required")
    original_text = "\n".join((notification.title, notification.body)).strip()
    body = _strip_text(notification.body, 400)
    age = extract_age(original_text)
    if _is_suppressed(age, original_text):
        return {"ok": True, "suppressed": True, "duplicate": False}
    if not body:
        body = "Notification received; no preview text was retained."
    captured_at = now or _now_text()
    location = extract_location(body, "")
    event_hash = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    dedupe_key = hashlib.sha256(f"grindr:{event_id}".encode("utf-8")).hexdigest()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    migrate(path)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        existing = connection.execute(
            "SELECT id FROM listings WHERE source = 'grindr' AND dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing:
            listing_id = int(existing["id"])
            connection.execute(
                "UPDATE listings SET text_excerpt = ?, last_seen_at = ?, locality = COALESCE(?, locality) WHERE id = ?",
                (body, captured_at, location, listing_id),
            )
            duplicate = True
            added = 0
            updated = 1
        else:
            cursor = connection.execute(
                "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, text_excerpt, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status) VALUES ('grindr', ?, ?, NULL, 'Grindr notification', ?, NULL, ?, ?, ?, NULL, '[]', 'new')",
                (dedupe_key, event_hash, body, captured_at, captured_at, location),
            )
            listing_id = int(cursor.lastrowid)
            duplicate = False
            added = 1
            updated = 0
        connection.execute(
            "INSERT INTO runs (source, started_at, finished_at, status, listings_seen, listings_added, listings_updated, error_summary) VALUES ('grindr', ?, ?, 'succeeded', 1, ?, ?, NULL)",
            (captured_at, captured_at, added, updated),
        )
        connection.commit()
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
        return {"ok": True, "listing_id": listing_id, "duplicate": duplicate, "suppressed": False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate(DB_PATH)
    yield


app = FastAPI(title="Grindr notification bridge", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/grindr/notifications")
async def receive_notification(request: Request):
    if not _authorized(request):
        raise HTTPException(401, "Unauthorized")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Payload too large")
    notification = _read_payload(request, body)
    result = _record_notification(notification, DB_PATH)
    return JSONResponse(result, status_code=202 if result.get("suppressed") else 200)
