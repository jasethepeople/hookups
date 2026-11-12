from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml
from dotenv import dotenv_values

from src.db import DEFAULT_DB_PATH, PROJECT_ROOT, migrate

WEIGHTS = {
    "distance": 25,
    "last_seen": 20,
    "age": 15,
    "body_position": 15,
    "hosts": 10,
    "photos": 10,
    "discretion": 5,
}
SCORE_CACHE_TTL = timedelta(days=30)
LAST_SEEN_WINDOW = timedelta(hours=72)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _whole_number(value: object) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _points(value: float, maximum: int) -> int:
    return max(0, min(maximum, math.floor(value + 0.5)))


def _terms(value: object) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        candidates = []
    output: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_phrase(candidate)
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return output


def _normalize_phrase(value: object) -> str:
    text = str(value or "").casefold().replace("_", " ")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized_text = f" {_normalize_phrase(text)} "
    return f" {phrase} " in normalized_text


def _record_text(record: dict[str, Any]) -> str:
    tags = record.get("tags")
    if isinstance(tags, str):
        tags_text = tags
    elif isinstance(tags, (list, tuple, set)):
        tags_text = " ".join(str(tag) for tag in tags)
    else:
        tags_text = ""
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "body", "location")
    ) + " " + tags_text


def _distance_points(record: dict[str, Any], profile: dict[str, Any]) -> int:
    distance = _number(record.get("distance_miles"))
    minutes = _number(profile.get("max_drive_minutes"))
    if distance is None or minutes is None or distance < 0 or minutes < 0:
        return 0
    maximum_miles = minutes * 0.5
    if maximum_miles == 0:
        return 25 if distance == 0 else 0
    return _points(25 * max(0.0, 1.0 - distance / maximum_miles), 25)


def _last_seen_points(record: dict[str, Any], now: datetime) -> int:
    last_seen = _timestamp(record.get("last_seen_at"))
    if last_seen is None:
        return 0
    elapsed = max(0.0, (now - last_seen).total_seconds())
    return _points(20 * max(0.0, 1.0 - elapsed / LAST_SEEN_WINDOW.total_seconds()), 20)


def _age_points(record: dict[str, Any], profile: dict[str, Any]) -> int:
    age = _whole_number(record.get("age"))
    age_range = profile.get("age_range")
    if age is None or age < 18 or not isinstance(age_range, dict):
        return 0
    minimum = _whole_number(age_range.get("min_age"))
    maximum = _whole_number(age_range.get("max_age"))
    if minimum is None or maximum is None or minimum < 18 or maximum < minimum:
        return 0
    return 15 if minimum <= age <= maximum else 0


def _preference_points(record: dict[str, Any], profile: dict[str, Any], text: str) -> int:
    preferences: list[str] = []
    for key in ("body_types", "positions"):
        preferences.extend(_terms(profile.get(key)))
    if not preferences:
        return 0
    matched = sum(_contains_phrase(text, term) for term in preferences)
    return _points(15 * matched / len(preferences), 15)


def _hosting_points(record: dict[str, Any], profile: dict[str, Any], text: str) -> int:
    mode = _normalize_phrase(profile.get("host_or_travel"))
    if not mode:
        return 0
    modes = {"host", "travel"} if mode == "either" else {mode}
    if not modes.intersection({"host", "travel"}):
        return 0
    tags = record.get("tags")
    tag_text = " " if not isinstance(tags, (list, tuple, set)) else " " + " ".join(str(tag) for tag in tags) + " "
    host_match = any(
        _contains_phrase(text, phrase) or _contains_phrase(tag_text, phrase)
        for phrase in ("can host", "hosting", "host at my place", "my place", "my home")
    ) or _contains_phrase(tag_text, "host")
    travel_match = any(
        _contains_phrase(text, phrase) or _contains_phrase(tag_text, phrase)
        for phrase in ("can travel", "travel to you", "come to you", "your place")
    ) or _contains_phrase(tag_text, "travel")
    return 10 if ("host" in modes and host_match) or ("travel" in modes and travel_match) else 0


def _discretion_points(profile: dict[str, Any], text: str) -> int:
    configured = _terms(profile.get("discretion_level"))
    if not configured:
        return 0
    signals = ("discreet", "discretion", "private", "low key", "anonymous", "no face", "no photos")
    for preference in configured:
        if any(_contains_phrase(preference, signal) and _contains_phrase(text, signal) for signal in signals):
            return 5
    return 0


def _dealbreaker(record: dict[str, Any], profile: dict[str, Any], text: str) -> str | None:
    for term in _terms(profile.get("dealbreakers")):
        if _contains_phrase(text, term):
            return term
    age = _whole_number(record.get("age"))
    return "under_18" if age is not None and age < 18 else None


def _components(record: dict[str, Any], profile: dict[str, Any], now: datetime) -> dict[str, int]:
    text = _record_text(record)
    photos = record.get("photos")
    has_photo = isinstance(photos, (list, tuple, set)) and any(str(photo).strip() for photo in photos)
    return {
        "distance": _distance_points(record, profile),
        "last_seen": _last_seen_points(record, now),
        "age": _age_points(record, profile),
        "body_position": _preference_points(record, profile, text),
        "hosts": _hosting_points(record, profile, text),
        "photos": 10 if has_photo else 0,
        "discretion": _discretion_points(profile, text),
    }


def _input_hash(record: dict[str, Any], profile: dict[str, Any], components: dict[str, int], model_url: str) -> str:
    stable_fields = {
        key: record.get(key)
        for key in ("id", "source", "title", "url", "body", "photos", "age", "distance_miles", "tags", "location", "last_seen_at", "superseded_by")
    }
    payload = {
        "record": stable_fields,
        "profile": profile,
        "components": components,
        "model_url_hash": hashlib.sha256(model_url.encode("utf-8")).hexdigest() if model_url else "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _two_sentences(text: str) -> str | None:
    cleaned = " ".join(str(text).strip().replace("\n", " ").split()).strip("`\"'")
    parts = re.findall(r"[^.!?]+[.!?]+(?=\s|$)", cleaned)
    if len(parts) != 2 or "".join(parts).strip() != cleaned:
        return None
    return " ".join(part.strip() for part in parts)


def _deterministic_why(score: int, components: dict[str, int], hard_zero: str | None) -> str:
    if hard_zero == "under_18":
        return "The record indicates an age under 18, so it receives 0/100. Age-ineligible listings are excluded from ranking."
    if hard_zero:
        return "A configured dealbreaker matched, so the score is 0/100. Positive signals cannot override a hard exclusion."
    labels = {
        "distance": "distance",
        "last_seen": "recently seen",
        "age": "age range",
        "body_position": "body-type and position matches",
        "hosts": "hosting/travel",
        "photos": "photos",
        "discretion": "discretion",
    }
    positive = [f"{labels[key]} ({components[key]}/{WEIGHTS[key]})" for key in WEIGHTS if components[key] > 0]
    if not positive:
        return "The listing scored 0/100 because no eligible score inputs matched. Missing or unmatched profile inputs contribute no points."
    strongest = ", ".join(positive)
    return f"The listing scored {score}/100 with points from {strongest}. Missing or unmatched inputs contribute no points."


def _response_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("why", "output", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("why") or value.get("text") or value.get("content")
            if isinstance(nested, str):
                return nested
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _model_why(model_url: str, score: int, components: dict[str, int]) -> str | None:
    prompt = (
        "Write exactly two concise sentences explaining this deterministic listing score. "
        "Use only the component values below; do not infer facts, mention people, or add personal details. "
        f"Score: {score}/100. Components: {json.dumps(components, sort_keys=True)}. "
        "Return only the two sentences."
    )
    try:
        response = httpx.post(model_url, json={"input": prompt}, timeout=15.0, follow_redirects=False)
        response.raise_for_status()
        return _two_sentences(_response_text(response))
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def _load_cached(connection: sqlite3.Connection, record_id: str, input_hash: str, now: datetime) -> tuple[int, dict[str, int], str, bool] | None:
    row = connection.execute(
        "SELECT input_hash, score, components_json, why, model_used, expires_at FROM score_cache WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row is None or row[0] != input_hash:
        return None
    expires = _timestamp(row[5])
    if expires is None or expires <= now:
        return None
    try:
        components = json.loads(row[2])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(components, dict):
        return None
    return int(row[1]), components, str(row[3]), bool(row[4])


def _score_one(
    record: dict[str, Any],
    profile: dict[str, Any],
    now: datetime,
    model_url: str,
    connection: sqlite3.Connection | None,
) -> dict[str, Any]:
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("normalized records require an id")
    components = _components(record, profile, now)
    hard_zero = _dealbreaker(record, profile, _record_text(record))
    score = 0 if hard_zero or record.get("superseded_by") else sum(components.values())
    input_hash = _input_hash(record, profile, components, model_url)
    if connection is not None:
        cached = _load_cached(connection, record_id, input_hash, now)
        if cached is not None:
            return {
                "id": record_id,
                "score": cached[0],
                "components": cached[1],
                "why": cached[2],
                "model_used": cached[3],
                "cache_hit": True,
                "hard_zero": bool(hard_zero or record.get("superseded_by")),
            }
    why = None if hard_zero or record.get("superseded_by") else (_model_why(model_url, score, components) if model_url else None)
    model_used = why is not None and bool(model_url)
    if why is None:
        why = _deterministic_why(score, components, hard_zero or ("superseded" if record.get("superseded_by") else None))
    result = {
        "id": record_id,
        "score": score,
        "components": components,
        "why": why,
        "model_used": model_used,
        "cache_hit": False,
        "hard_zero": bool(hard_zero or record.get("superseded_by")),
    }
    if connection is not None:
        expires_at = (now + SCORE_CACHE_TTL).isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO score_cache (record_id, input_hash, score, components_json, why, model_used, scored_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id) DO UPDATE SET input_hash=excluded.input_hash, score=excluded.score, "
            "components_json=excluded.components_json, why=excluded.why, model_used=excluded.model_used, "
            "scored_at=excluded.scored_at, expires_at=excluded.expires_at",
            (
                record_id,
                input_hash,
                score,
                json.dumps(components, sort_keys=True, separators=(",", ":")),
                why,
                int(model_used),
                now.isoformat(timespec="seconds"),
                expires_at,
            ),
        )
    return result


def load_profile(path: str | Path | None = None) -> dict[str, Any]:
    profile_path = Path(path) if path else PROJECT_ROOT / "config" / "profile.yaml"
    try:
        value = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}


def model_url_from_env() -> str:
    values = dotenv_values(PROJECT_ROOT / ".env")
    return str(values.get("MODEL_URL") or "").strip()


def score_records(
    records: Iterable[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    now: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    cache: bool = True,
    model_url: str | None = None,
) -> list[dict[str, Any]]:
    current = _now(now)
    selected_profile = profile if isinstance(profile, dict) else load_profile()
    endpoint = model_url_from_env() if model_url is None else model_url.strip()
    if not endpoint:
        endpoint = ""
    rows = list(records)
    if not cache:
        return [_score_one(record, selected_profile, current, endpoint, None) for record in rows]
    migrate(db_path)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("DELETE FROM score_cache WHERE expires_at <= ?", (current.isoformat(timespec="seconds"),))
        results = [_score_one(record, selected_profile, current, endpoint, connection) for record in rows]
        connection.commit()
        return results
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def score_record(
    record: dict[str, Any],
    profile: dict[str, Any] | None = None,
    now: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    cache: bool = True,
    model_url: str | None = None,
) -> dict[str, Any]:
    return score_records([record], profile, now, db_path, cache, model_url)[0]
