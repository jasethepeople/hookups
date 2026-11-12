from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = {"fbclid", "gclid", "ref", "ref_source", "source", "mc_cid", "mc_eid"}
AGE_PATTERNS = (
    re.compile(r"\b(?:age|aged)\s*(?:is\s*)?[:=]?\s*(\d{1,3})(?!\d)(?!\s*\+)", re.I),
    re.compile(r"\b(\d{1,3})\s*(?:years?\s+old|y\s*/?\s*o|yo)\b", re.I),
    re.compile(r"\b(?:i['’]m|i am)\s+(\d{1,3})(?!\d)(?!\s*\+)", re.I),
    re.compile(r"^\s*\[\s*(\d{1,3})\s*\](?!\s*\+)", re.M),
)
LOCATION_LABEL = re.compile(
    r"(?im)^\s*(?:location|city|based\s+in|located\s+in|near)\s*[:=-]\s*([^\n|;]{2,80}?)\s*$"
)
R4R_BRACKET_LOCATION = re.compile(
    r"^\s*\[\s*\d{1,3}\s*\]\s*\[[^\]]{1,16}\]\s*\[\s*([^\]]{2,60}?)\s*\]"
)
NATURAL_LOCATION = re.compile(
    r"\b(?:based\s+in|located\s+in|near|around|in)\s+"
    r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})"
    r"(?=\s*(?:[.,;|\n]|$))"
)
DISTANCE_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d{1,2})?)\s*(?:mi|miles?)\s+(?:away|from\s+me)\b", re.I)
RELATIVE_TIME = re.compile(r"^\s*(\d+)\s*(minute|min|hour|hr|day|week)s?\s+ago\s*$", re.I)
BODY_TYPES = {
    "athletic": re.compile(r"\b(?:athletic|fit(?:\s+build)?)\b", re.I),
    "slim": re.compile(r"\b(?:slim|slender)\b", re.I),
    "average": re.compile(r"\b(?:average\s+build|average-bodied)\b", re.I),
    "curvy": re.compile(r"\b(?:curvy|curvaceous)\b", re.I),
    "stocky": re.compile(r"\bstocky\b", re.I),
    "muscular": re.compile(r"\b(?:muscular|muscle[- ]?bound)\b", re.I),
    "dad_bod": re.compile(r"\bdad[ -]?bod\b", re.I),
    "thick": re.compile(r"\b(?:thick|thicc)\b", re.I),
    "chubby": re.compile(r"\bchubby\b", re.I),
    "petite": re.compile(r"\bpetite\b", re.I),
    "toned": re.compile(r"\btoned\b", re.I),
}
UNDERAGE_PATTERN = re.compile(r"\b(?:under\s+(?:the\s+)?18|underage|i['’]m\s+(?:a\s+)?minor|i am\s+(?:a\s+)?minor)\b", re.I)
NONCONSENT_PATTERNS = (
    re.compile(r"\bnon[-\s]?consensual\b", re.I),
    re.compile(r"\bwithout\s+(?:my\s+|their\s+|any\s+)?consent\b", re.I),
    re.compile(r"\bcoerc(?:e|ion|ive)\b", re.I),
    re.compile(r"\bforced\s+(?:sex|sexual\s+contact|to\s+\w+)\b", re.I),
)


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _clean_body(value: object) -> str:
    if value is None:
        return ""
    return "\n".join(" ".join(line.split()) for line in str(value).splitlines()).strip()


def _canonical_url(value: object) -> str:
    raw_url = _clean_text(value)
    parts = urlsplit(raw_url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute HTTP or HTTPS URL")
    if parts.username or parts.password:
        raise ValueError("url must not contain credentials")
    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("url contains an invalid port") from error
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(query, doseq=True), ""))


def extract_age(text: str) -> int | None:
    for pattern in AGE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            age = int(match.group(1))
            if 0 <= age <= 120:
                return age
    return None


def extract_location(text: str, title: str = "") -> str | None:
    for source_text in (text or "", title or ""):
        match = LOCATION_LABEL.search(source_text)
        if match:
            return _clean_text(match.group(1)).strip(" .,-") or None
    bracket = R4R_BRACKET_LOCATION.search(title or "")
    if bracket:
        location = _clean_text(bracket.group(1)).strip(" .,-")
        if location and not re.fullmatch(r"\d{1,3}|[A-Za-z]\s*4\s*[A-Za-z]", location, re.I):
            return location
    for source_text in (title or "", text or ""):
        match = NATURAL_LOCATION.search(source_text)
        if match:
            return _clean_text(match.group(1)).strip(" .,-") or None
    return None


def _parse_datetime(value: object, now: datetime) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = _clean_text(value)
    if not text:
        return None
    relative = RELATIVE_TIME.fullmatch(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }[unit]
        return (now - delta).isoformat(timespec="seconds")
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%b %d, %Y", "%B %d, %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _numeric_distance(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return None
    return distance if 0 <= distance <= 10000 else None


def _photos(raw: dict) -> list[str]:
    values = raw.get("photos")
    if values is None:
        values = [raw.get("photo_url"), raw.get("thumb"), raw.get("thumbnail")]
    elif isinstance(values, (str, dict)):
        values = [values]
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("url") or value.get("src")
        if not isinstance(value, str):
            continue
        try:
            photo_url = _canonical_url(value)
        except ValueError:
            continue
        if photo_url not in result:
            result.append(photo_url)
    return result


def _source_tags(value: object) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,|]", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return sorted({_clean_text(item).lower() for item in values if _clean_text(item)})


def _is_suppressed(age: int | None, text: str) -> bool:
    if age is not None and age < 18:
        return True
    if UNDERAGE_PATTERN.search(text):
        return True
    return any(pattern.search(text) for pattern in NONCONSENT_PATTERNS)


def normalize_record(raw: dict, now: datetime | None = None) -> dict | None:
    if not isinstance(raw, dict):
        raise TypeError("raw listing must be a dictionary")
    source = _clean_text(raw.get("source"))
    if not source:
        raise ValueError("source is required")
    url = _canonical_url(raw.get("url"))
    title = _clean_text(raw.get("title"))
    body = _clean_body(raw.get("body") if raw.get("body") is not None else raw.get("text_excerpt"))
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    searchable_text = "\n".join((title, body))
    age_value = raw.get("age")
    if isinstance(age_value, int) and not isinstance(age_value, bool) and 0 <= age_value <= 120:
        age = age_value
    elif isinstance(age_value, str):
        age = extract_age(age_value)
    else:
        age = None
    if age is None:
        age = extract_age(title)
    if age is None:
        age = extract_age(body)
    location_value = raw.get("location") if raw.get("location") is not None else raw.get("locality")
    location = _clean_text(location_value) or extract_location(body, title)
    distance = _numeric_distance(raw.get("distance_miles"))
    if distance is None:
        distance_match = DISTANCE_PATTERN.search(searchable_text)
        distance = float(distance_match.group(1)) if distance_match else None
    tags = set(_source_tags(raw.get("tags")))
    for body_type, pattern in BODY_TYPES.items():
        if pattern.search(searchable_text):
            tags.add(f"body_type:{body_type}")
    posted_value = raw.get("posted_at")
    if posted_value is None:
        posted_value = raw.get("posted") if raw.get("posted") is not None else raw.get("created_utc")
    posted_at = _parse_datetime(posted_value, now_utc)
    last_seen_at = _parse_datetime(raw.get("last_seen_at"), now_utc) or now_utc.isoformat(timespec="seconds")
    raw_copy = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
    record = {
        "id": hashlib.sha256(f"{source}{url}".encode("utf-8")).hexdigest(),
        "source": source,
        "title": title,
        "url": url,
        "body": body,
        "photos": _photos(raw),
        "age": age,
        "location": location or None,
        "distance_miles": distance,
        "tags": sorted(tags),
        "posted_at": posted_at,
        "last_seen_at": last_seen_at,
        "raw": raw_copy,
    }
    return None if _is_suppressed(age, searchable_text) else record
