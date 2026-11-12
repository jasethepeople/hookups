from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.lexbor import LexborHTMLParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import DEFAULT_DB_PATH, migrate

PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
GAPS_PATH = PROJECT_ROOT / "docs" / "GAPS.md"
SOURCE = "locanto"
BASE_URL = "https://www.locanto.com"
CACHE_TTL = timedelta(minutes=30)
POLL_FLOOR = timedelta(minutes=30)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}
CARD_SELECTOR = "article, [data-adid], [data-ad-id], .resultbox, .result-box, .result-item, .ad-item, .ad-card, .listing-card, [class*='classified-ad']"
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "cf-mitigated",
    "content-length",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "server",
}


def utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return utc_now(value).isoformat(timespec="seconds")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_profile(profile_path: str | Path = PROFILE_PATH) -> dict:
    with Path(profile_path).open("r", encoding="utf-8") as file:
        profile = yaml.safe_load(file) or {}
    if not isinstance(profile, dict):
        raise ValueError("Profile YAML must contain a mapping at its top level")
    return profile


def build_search_urls(profile: dict) -> list[str]:
    config = profile.get("locanto") or {}
    if not isinstance(config, dict):
        raise ValueError("profile.locanto must be a mapping")
    city_slug = config.get("city_slug")
    categories = config.get("categories") or []
    if not city_slug or not categories:
        return []
    if not isinstance(city_slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", city_slug.strip()):
        raise ValueError("locanto.city_slug must be one Locanto path slug")
    if not isinstance(categories, list):
        raise ValueError("locanto.categories must be a list of category paths")
    city = quote(city_slug.strip(), safe="-._~")
    urls = []
    for category in categories:
        if not isinstance(category, str):
            raise ValueError("Each Locanto category must be a string path")
        parts = [part for part in category.strip().strip("/").split("/") if part]
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts):
            raise ValueError(f"Invalid Locanto category path: {category!r}")
        encoded_category = "/".join(quote(part, safe="-._~") for part in parts)
        urls.append(f"{BASE_URL}/{city}/{encoded_category}")
    return list(dict.fromkeys(urls))


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def text_of(node) -> str:
    if node is None:
        return ""
    return " ".join(node.text(separator=" ", strip=True).split())


def value_of(node) -> str | None:
    if node is None:
        return None
    for key in ("content", "datetime", "data-value", "value"):
        value = node.attrs.get(key)
        if value and value.strip():
            return value.strip()
    value = text_of(node)
    return value or None


def first_value(node, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        found = node.css_first(selector)
        value = value_of(found)
        if value:
            return value
    return None


def absolute_locanto_url(base_url: str, href: str | None) -> str | None:
    if not href or href.strip().startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = canonicalize_url(urljoin(base_url, href.strip()))
    host = (urlsplit(absolute).hostname or "").lower()
    if host != "locanto.com" and not host.endswith(".locanto.com"):
        return None
    return absolute


def listing_anchor(node, base_url: str, search_url: str):
    anchors = []
    if getattr(node, "tag", "") == "a":
        anchors.append(node)
    for selector in (
        "h2 a[href]",
        "h3 a[href]",
        "[class*='title'] a[href]",
        "a[href*='ID_']",
        "a[href*='/ad/']",
        "a[href]",
    ):
        anchors.extend(node.css(selector))
    seen = set()
    for anchor in anchors:
        href = anchor.attrs.get("href")
        url = absolute_locanto_url(base_url, href)
        if not url or url == search_url or url in seen:
            continue
        seen.add(url)
        path = urlsplit(url).path.lower()
        if path.startswith("/g/") or any(part in path for part in ("/login", "/register", "/contact", "/post-ad")):
            continue
        return anchor, url
    return None, None


def parse_listings(html: str, base_url: str, search_url: str | None = None) -> list[dict]:
    base = canonicalize_url(base_url)
    search = canonicalize_url(search_url or base_url)
    parser = LexborHTMLParser(html)
    candidates = parser.css(CARD_SELECTOR)
    if not candidates:
        candidates = parser.css("a[href*='ID_'], a[href*='/ad/']")
    unique: dict[str, dict] = {}
    for card in candidates:
        anchor, url = listing_anchor(card, base, search)
        if not url:
            continue
        title = first_value(card, ("h2", "h3", "[class*='title']")) or text_of(anchor)
        if not title:
            continue
        price = first_value(card, ("[itemprop='price']", "[class*='price']", "[data-price]"))
        location = first_value(card, ("[itemprop='addressLocality']", "[class*='location']", "[class*='ad-location']"))
        posted = first_value(card, ("time[datetime]", "[data-posted]", "[class*='posted']", "[class*='date']", "[class*='time']"))
        image = None
        for image_node in card.css("img[data-src], img[data-original], img[src]"):
            raw_image = image_node.attrs.get("data-src") or image_node.attrs.get("data-original") or image_node.attrs.get("src")
            if raw_image and not raw_image.strip().lower().startswith("data:"):
                image = urljoin(base, raw_image.strip())
                break
        source_id = None
        for attribute in ("data-adid", "data-ad-id", "data-id"):
            if card.attrs.get(attribute):
                source_id = card.attrs[attribute].strip()
                break
        if not source_id:
            match = re.search(r"(?:^|/)ID_([A-Za-z0-9_-]+)", urlsplit(url).path)
            if match:
                source_id = match.group(1)
        excerpt = text_of(card)[:1000]
        item = {
            "source": SOURCE,
            "source_id": source_id,
            "dedupe_key": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "title": title[:500],
            "url": url,
            "price": price,
            "location": location,
            "posted": posted,
            "thumb": image,
            "text_excerpt": excerpt,
        }
        if url not in unique:
            unique[url] = item
        else:
            for key, value in item.items():
                if unique[url].get(key) in (None, "") and value not in (None, ""):
                    unique[url][key] = value
    return list(unique.values())


def _new_client() -> httpx.Client:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    proxy = os.environ.get("PROXY_URL", "").strip()
    options = {
        "headers": REQUEST_HEADERS,
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "follow_redirects": True,
        "max_redirects": 5,
    }
    if proxy:
        options["proxy"] = proxy
    return httpx.Client(**options)


def _cache_row(connection: sqlite3.Connection, url: str, now: datetime) -> tuple | None:
    row = connection.execute(
        "SELECT fetched_at, status_code, final_url, headers_json, body, error_summary FROM http_cache WHERE source = ? AND url = ?",
        (SOURCE, url),
    ).fetchone()
    if not row:
        return None
    fetched_at = parse_timestamp(row[0])
    if fetched_at is None or not timedelta(0) <= now - fetched_at < CACHE_TTL:
        return None
    return row


def _store_cache(connection: sqlite3.Connection, url: str, fetched_at: str, status_code: int, final_url: str, headers: dict, body: str, error: str | None) -> None:
    connection.execute(
        "INSERT INTO http_cache (source, url, fetched_at, status_code, final_url, headers_json, body, error_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, url) DO UPDATE SET fetched_at = excluded.fetched_at, status_code = excluded.status_code, final_url = excluded.final_url, headers_json = excluded.headers_json, body = excluded.body, error_summary = excluded.error_summary",
        (SOURCE, url, fetched_at, status_code, final_url, json.dumps(headers, sort_keys=True), body, error),
    )
    connection.commit()


def _last_source_start(connection: sqlite3.Connection) -> datetime | None:
    row = connection.execute(
        "SELECT started_at FROM runs WHERE source = ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (SOURCE,),
    ).fetchone()
    return parse_timestamp(row[0]) if row else None


def _parse_cached(row: tuple, search_url: str) -> list[dict]:
    fetched_at, status_code, final_url, headers_json, body, error = row
    if not 200 <= status_code < 300 or not body:
        return []
    return parse_listings(body, final_url or search_url, search_url)


def _persist_listings(connection: sqlite3.Connection, listings: list[dict], now_text: str) -> int:
    inserted_or_updated = 0
    for item in listings:
        cursor = connection.execute(
            "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, price_text, text_excerpt, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status, thumb_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', 'new', ?) ON CONFLICT(source, dedupe_key) DO UPDATE SET source_id = COALESCE(excluded.source_id, listings.source_id), canonical_url = excluded.canonical_url, title = excluded.title, price_text = COALESCE(excluded.price_text, listings.price_text), text_excerpt = excluded.text_excerpt, posted_at = COALESCE(excluded.posted_at, listings.posted_at), last_seen_at = excluded.last_seen_at, locality = COALESCE(excluded.locality, listings.locality), thumb_url = COALESCE(excluded.thumb_url, listings.thumb_url)",
            (SOURCE, item["dedupe_key"], item.get("source_id"), item["url"], item["title"], item.get("price"), item.get("text_excerpt", ""), item.get("posted"), now_text, now_text, item.get("location"), item.get("thumb")),
        )
        inserted_or_updated += max(cursor.rowcount, 0)
    connection.commit()
    return inserted_or_updated


def _append_gap(message: str, now: datetime) -> None:
    line = f"- {now.date().isoformat()} Locanto collection: {message}."
    try:
        current = GAPS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# Gaps\n"
    if line not in current:
        with GAPS_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")


def collect(
    profile_path: str | Path = PROFILE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    dry: bool = False,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict:
    current = utc_now(now)
    profile = load_profile(profile_path)
    urls = build_search_urls(profile)
    migrate(db_path)
    database = sqlite3.connect(str(db_path), timeout=30)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("DELETE FROM http_cache WHERE fetched_at < ?", ((current - CACHE_TTL).isoformat(timespec="seconds"),))
    database.commit()
    if not urls:
        database.close()
        return {"count": 0, "first_3": [], "listings": [], "notice": "Set locanto.city_slug and locanto.categories in config/profile.yaml; no request was made."}
    cached = {url: _cache_row(database, url, current) for url in urls}
    pending = [url for url in urls if cached[url] is None]
    last_start = _last_source_start(database)
    if pending and last_start and current - last_start < POLL_FLOOR:
        listings = []
        for url in urls:
            if cached[url]:
                listings.extend(_parse_cached(cached[url], url))
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        if not dry:
            _persist_listings(database, listings, iso_utc(current))
        database.close()
        wait_until = (last_start + POLL_FLOOR).isoformat(timespec="seconds")
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": f"Source poll floor active until {wait_until}; no network request was made."}
    run_id = None
    fetched_any = False
    failures = []
    blocked = False
    if pending:
        cursor = database.execute(
            "INSERT INTO runs (source, started_at, finished_at, status, listings_seen, listings_added, listings_updated, error_summary) VALUES (?, ?, NULL, 'running', 0, 0, 0, NULL)",
            (SOURCE, iso_utc(current)),
        )
        run_id = cursor.lastrowid
        database.commit()
        own_client = client is None
        client_context = _new_client() if own_client else nullcontext(client)
        with client_context as active_client:
            for url in pending:
                fetched_any = True
                try:
                    response = active_client.get(url)
                    headers = {name: response.headers.get(name, "") for name in SAFE_RESPONSE_HEADERS if response.headers.get(name) is not None}
                    error = None
                    body = response.text if len(response.content) <= MAX_RESPONSE_BYTES else ""
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        error = "response_too_large"
                    _store_cache(database, url, iso_utc(current), response.status_code, str(response.url), headers, body, error)
                    row = _cache_row(database, url, current)
                    cached[url] = row
                    if response.status_code == 403 or response.status_code == 429 or headers.get("cf-mitigated"):
                        blocked = True
                        failures.append(f"http_{response.status_code}_challenge")
                        _append_gap(f"HTTP {response.status_code} challenge; retry deferred by the 30-minute source floor", current)
                        break
                    if not 200 <= response.status_code < 300:
                        failures.append(f"http_{response.status_code}")
                    if error:
                        failures.append(error)
                except httpx.RequestError as error:
                    summary = f"request_error_{type(error).__name__}"
                    _store_cache(database, url, iso_utc(current), 0, url, {}, "", summary)
                    cached[url] = _cache_row(database, url, current)
                    failures.append(summary)
                    _append_gap(f"request failed ({type(error).__name__}); retry deferred by the 30-minute source floor", current)
        listings = []
        for url in urls:
            if cached.get(url):
                listings.extend(_parse_cached(cached[url], url))
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        added = 0 if dry else _persist_listings(database, listings, iso_utc(current))
        successful_responses = sum(1 for url in pending if cached.get(url) and 200 <= cached[url][1] < 300 and cached[url][4])
        if blocked and successful_responses == 0:
            status = "blocked"
        elif failures and successful_responses:
            status = "partial"
        elif failures:
            status = "failed"
        else:
            status = "succeeded"
        summary = ", ".join(dict.fromkeys(failures)) or None
        database.execute(
            "UPDATE runs SET finished_at = ?, status = ?, listings_seen = ?, listings_added = ?, error_summary = ? WHERE id = ?",
            (iso_utc(current), status, len(listings), added, summary, run_id),
        )
        database.commit()
    else:
        listings = []
        for url in urls:
            if cached[url]:
                listings.extend(_parse_cached(cached[url], url))
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        if not dry:
            _persist_listings(database, listings, iso_utc(current))
    database.close()
    notice = None
    if failures:
        notice = "; ".join(dict.fromkeys(failures))
    elif not fetched_any and pending:
        notice = "No response was fetched."
    return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": notice}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Fetch and cache, print results, but do not save listing rows")
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    try:
        result = collect(profile_path=args.profile, db_path=args.db, dry=args.dry)
    except (OSError, ValueError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"Locanto run failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"count: {result['count']}")
    print("first 3:")
    print(json.dumps(result["first_3"], indent=2, ensure_ascii=False))
    if result.get("notice"):
        print(f"notice: {result['notice']}")
    if args.dry:
        print("dry: listing rows not saved; URL cache and run state remain active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
