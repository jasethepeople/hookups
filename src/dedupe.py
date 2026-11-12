from __future__ import annotations

import math
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable


DISTANCE_BUCKET_MILES = 5.0
BODY_OVERLAP_THRESHOLD = 0.80
TIME_WINDOW = timedelta(hours=24)
TRIGRAM_SIZE = 3


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(character if character.isalnum() or character.isspace() else " " for character in text)
    return " ".join(text.split())


def _trigrams(value: object) -> set[str]:
    text = _normalized_text(value)
    if len(text) < TRIGRAM_SIZE:
        return set()
    return {text[index:index + TRIGRAM_SIZE] for index in range(len(text) - TRIGRAM_SIZE + 1)}


def trigram_jaccard(left: object, right: object) -> float:
    left_grams = _trigrams(left)
    right_grams = _trigrams(right)
    union = left_grams | right_grams
    if not union:
        return 0.0
    return len(left_grams & right_grams) / len(union)


def _photo_urls(record: dict) -> set[str]:
    photos = record.get("photos") or []
    if isinstance(photos, str):
        photos = [photos]
    if not isinstance(photos, (list, tuple, set)):
        return set()
    return {photo.strip() for photo in photos if isinstance(photo, str) and photo.strip()}


def _age(record: dict) -> int | None:
    value = record.get("age")
    if isinstance(value, bool) or value is None:
        return None
    try:
        age = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and value != age:
        return None
    return age if 18 <= age <= 120 else None


def _distance_bucket(record: dict) -> int | None:
    value = record.get("distance_miles")
    if isinstance(value, bool) or value is None:
        return None
    try:
        distance = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(distance) or distance < 0:
        return None
    return math.floor(distance / DISTANCE_BUCKET_MILES)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recency(record: dict) -> datetime | None:
    return _timestamp(record.get("posted_at")) or _timestamp(record.get("last_seen_at"))


def _is_cross_post(left: dict, right: dict) -> bool:
    left_source = str(left.get("source") or "").strip().casefold()
    right_source = str(right.get("source") or "").strip().casefold()
    if not left_source or not right_source or left_source == right_source:
        return False
    if _photo_urls(left) & _photo_urls(right):
        return True
    left_age = _age(left)
    right_age = _age(right)
    left_distance = _distance_bucket(left)
    right_distance = _distance_bucket(right)
    if left_age is None or left_age != right_age or left_distance is None or left_distance != right_distance:
        return False
    left_posted = _timestamp(left.get("posted_at"))
    right_posted = _timestamp(right.get("posted_at"))
    if left_posted is None or right_posted is None or abs(left_posted - right_posted) > TIME_WINDOW:
        return False
    return trigram_jaccard(left.get("body"), right.get("body")) >= BODY_OVERLAP_THRESHOLD


def _sort_key(record: dict) -> tuple[int, float, str]:
    timestamp = _recency(record)
    if timestamp is None:
        return (1, 0.0, str(record["id"]))
    return (0, -timestamp.timestamp(), str(record["id"]))


def dedupe_records(records: Iterable[dict]) -> list[dict]:
    items = records if isinstance(records, list) else list(records)
    for record in items:
        if not isinstance(record, dict):
            raise TypeError("each normalized record must be a dictionary")
        if not isinstance(record.get("id"), str) or not record["id"].strip():
            raise ValueError("each normalized record must have a non-empty id")
        record["superseded_by"] = None
    ordered = sorted(items, key=_sort_key)
    winners: list[dict] = []
    for candidate in ordered:
        match = next((winner for winner in winners if _is_cross_post(candidate, winner)), None)
        if match is None:
            winners.append(candidate)
        else:
            candidate["superseded_by"] = match["id"]
    return items
