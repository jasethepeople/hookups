from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
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
from src.sources.browser import fetch_with_browser, is_challenge
from src.sources.retry import RETRYABLE_STATUS_CODES, with_retries

PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
GAPS_PATH = PROJECT_ROOT / "docs" / "GAPS.md"
PROFILE_DIR = PROJECT_ROOT / "cache" / "squirt-profile"
SOURCE = "squirt"
BASE_URL = "https://www.squirt.org/"
CACHE_TTL = timedelta(minutes=30)
POLL_FLOOR = timedelta(minutes=30)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control", "cf-mitigated", "content-length", "content-type", "date",
    "etag", "last-modified", "location", "retry-after", "server",
}
CARD_SELECTOR = "article, [data-listing-id], [data-listingid], .cruising-listing, .cruise-listing, .listing-card, [class*='cruising-listing'], [class*='listing-row'], [class*='result-item']"


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


def build_listing_urls(profile: dict) -> list[str]:
    config = profile.get("squirt") or {}
    if not isinstance(config, dict):
        raise ValueError("profile.squirt must be a mapping")
    url = config.get("listings_url")
    if not url:
        return []
    if not isinstance(url, str):
        raise ValueError("squirt.listings_url must be a full HTTPS URL")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or (host != "squirt.org" and not host.endswith(".squirt.org")):
        raise ValueError("squirt.listings_url must use HTTPS on squirt.org or one of its subdomains")
    if parts.username or parts.password:
        raise ValueError("Credentials must not be embedded in squirt.listings_url")
    if parts.path in {"", "/"}:
        raise ValueError("squirt.listings_url must be a listings page, not the homepage")
    sensitive_query_keys = {"token", "auth", "key", "session", "sid", "cookie"}
    if any(key.lower() in sensitive_query_keys for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError("Credentials must be stored in .env, not in squirt.listings_url")
    return [canonicalize_url(url.strip())]


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


def absolute_listing_url(base_url: str, href: str | None) -> str | None:
    if not href or href.strip().lower().startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = canonicalize_url(urljoin(base_url, href.strip()))
    host = (urlsplit(absolute).hostname or "").lower()
    if host != "squirt.org" and not host.endswith(".squirt.org"):
        return None
    return absolute


def parse_listings(html: str, base_url: str, search_url: str | None = None) -> list[dict]:
    base = canonicalize_url(base_url)
    search = canonicalize_url(search_url or base_url)
    parser = LexborHTMLParser(html)
    cards = parser.css(CARD_SELECTOR)
    if not cards:
        cards = parser.css("a[href]")
    results: dict[str, dict] = {}
    for card in cards:
        anchors = []
        if getattr(card, "tag", "") == "a":
            anchors.append(card)
        for selector in ("h2 a[href]", "h3 a[href]", "[class*='title'] a[href]", "a[href]"):
            anchors.extend(card.css(selector))
        anchor = None
        listing_url = None
        for candidate in anchors:
            candidate_url = absolute_listing_url(base, candidate.attrs.get("href"))
            if candidate_url and candidate_url != search and candidate_url != base:
                path = urlsplit(candidate_url).path.lower()
                if any(part in path for part in ("/login", "/register", "/join", "/sign-in", "/account")):
                    continue
                anchor = candidate
                listing_url = candidate_url
                break
        if not listing_url:
            continue
        title = first_value(card, ("h2", "h3", "[itemprop='name']", "[class*='title']")) or text_of(anchor)
        if not title:
            continue
        location = first_value(card, ("[itemprop='addressLocality']", "[itemprop='address']", "[class*='location']", "[class*='address']"))
        posted = first_value(card, ("time[datetime]", "[data-posted]", "[class*='posted']", "[class*='date']", "[class*='time']"))
        thumb = None
        for image in card.css("img[data-src], img[data-original], img[src]"):
            candidate = image.attrs.get("data-src") or image.attrs.get("data-original") or image.attrs.get("src")
            if candidate and not candidate.strip().lower().startswith("data:"):
                thumb = urljoin(base, candidate.strip())
                break
        source_id = None
        for key in ("data-listing-id", "data-listingid", "data-id"):
            if card.attrs.get(key):
                source_id = card.attrs[key].strip()
                break
        if not source_id:
            match = re.search(r"(?:^|/)(?:listing|place|cruise)[/-]?([A-Za-z0-9_-]+)", urlsplit(listing_url).path, re.I)
            if match:
                source_id = match.group(1)
        results[listing_url] = {
            "source": SOURCE,
            "source_id": source_id,
            "dedupe_key": hashlib.sha256(listing_url.encode("utf-8")).hexdigest(),
            "title": title[:500],
            "url": listing_url,
            "price": None,
            "location": location,
            "posted": posted,
            "thumb": thumb,
            "text_excerpt": text_of(card)[:1000],
        }
    return list(results.values())


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


def _store_cache(connection: sqlite3.Connection, url: str, fetched_at: str, status: int, final_url: str, headers: dict, body: str, error: str | None) -> None:
    connection.execute(
        "INSERT INTO http_cache (source, url, fetched_at, status_code, final_url, headers_json, body, error_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, url) DO UPDATE SET fetched_at = excluded.fetched_at, status_code = excluded.status_code, final_url = excluded.final_url, headers_json = excluded.headers_json, body = excluded.body, error_summary = excluded.error_summary",
        (SOURCE, url, fetched_at, status, final_url, json.dumps(headers, sort_keys=True), body, error),
    )
    connection.commit()


def _append_gap(message: str, now: datetime) -> None:
    line = f"- {now.date().isoformat()} Squirt collection: {message}."
    try:
        current = GAPS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# Gaps\n"
    if line not in current:
        with GAPS_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")


def _persist_listings(connection: sqlite3.Connection, listings: list[dict], now_text: str) -> int:
    changed = 0
    for item in listings:
        cursor = connection.execute(
            "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, price_text, text_excerpt, thumb_url, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, '[]', 'new') ON CONFLICT(source, dedupe_key) DO UPDATE SET source_id = COALESCE(excluded.source_id, listings.source_id), canonical_url = excluded.canonical_url, title = excluded.title, text_excerpt = excluded.text_excerpt, thumb_url = COALESCE(excluded.thumb_url, listings.thumb_url), posted_at = COALESCE(excluded.posted_at, listings.posted_at), last_seen_at = excluded.last_seen_at, locality = COALESCE(excluded.locality, listings.locality)",
            (SOURCE, item["dedupe_key"], item.get("source_id"), item["url"], item["title"], item.get("text_excerpt", ""), item.get("thumb"), item.get("posted"), now_text, now_text, item.get("location")),
        )
        changed += max(cursor.rowcount, 0)
    connection.commit()
    return changed


def _response_headers(headers) -> dict:
    return {key: headers.get(key, "") for key in SAFE_RESPONSE_HEADERS if headers.get(key) is not None}


def collect(
    profile_path: str | Path = PROFILE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    dry: bool = False,
    probe_homepage: bool = False,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict:
    current = utc_now(now)
    profile = load_profile(profile_path)
    urls = build_listing_urls(profile)
    probe_only = not urls and probe_homepage
    if probe_only:
        urls = [BASE_URL]
    migrate(db_path)
    database = sqlite3.connect(str(db_path), timeout=30)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("DELETE FROM http_cache WHERE fetched_at < ?", ((current - CACHE_TTL).isoformat(timespec="seconds"),))
    database.commit()
    if not urls:
        database.close()
        return {"count": 0, "first_3": [], "listings": [], "notice": "Set squirt.listings_url to an explicit public listings page; no request was made."}
    cached = {url: _cache_row(database, url, current) for url in urls}
    pending = [url for url in urls if cached[url] is None]
    last = database.execute("SELECT started_at FROM runs WHERE source = ? ORDER BY started_at DESC, id DESC LIMIT 1", (SOURCE,)).fetchone()
    last_start = parse_timestamp(last[0]) if last else None
    if pending and last_start and current - last_start < POLL_FLOOR:
        listings = [item for url in urls if cached[url] and cached[url][1] == 200 for item in parse_listings(cached[url][4], cached[url][2], url)]
        if not dry and not probe_only:
            _persist_listings(database, listings, iso_utc(current))
        wait_until = (last_start + POLL_FLOOR).isoformat(timespec="seconds")
        database.close()
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": f"Source poll floor active until {wait_until}; no network request was made."}
    if not pending:
        listings = [item for url in urls if cached[url] and cached[url][1] == 200 for item in parse_listings(cached[url][4], cached[url][2], url)]
        if not dry and not probe_only:
            _persist_listings(database, listings, iso_utc(current))
        database.close()
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": "Fresh URL response cache used; no network request was made."}
    cursor = database.execute(
        "INSERT INTO runs (source, started_at, finished_at, status, listings_seen, listings_added, listings_updated, error_summary) VALUES (?, ?, NULL, 'running', 0, 0, 0, NULL)",
        (SOURCE, iso_utc(current)),
    )
    run_id = cursor.lastrowid
    database.commit()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None
    own_client = client is None
    options = {"headers": REQUEST_HEADERS, "timeout": httpx.Timeout(20.0, connect=10.0), "follow_redirects": True, "max_redirects": 6}
    client_context = httpx.Client(**options) if own_client else nullcontext(client)
    failures = []
    blocked = False
    started = time.monotonic()
    deadline = started + 600
    try:
        with client_context as active_client:
            for url in pending:
                if time.monotonic() >= deadline:
                    failures.append("source_time_cap_reached")
                    break
                try:
                    response = with_retries(
                        lambda: active_client.get(url),
                        should_retry=lambda item: item.status_code in RETRYABLE_STATUS_CODES,
                        retry_exceptions=(httpx.RequestError,),
                    )
                    headers = _response_headers(response.headers)
                    body = response.text if len(response.content) <= MAX_RESPONSE_BYTES else ""
                    error = None if body or not response.content else "response_too_large"
                    final_url = str(response.url)
                    status = response.status_code
                    challenge = is_challenge(status, "", body, headers)
                    if challenge or error:
                        if time.monotonic() < deadline:
                            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                            browser = fetch_with_browser(
                                url,
                                PROFILE_DIR,
                                deadline,
                                proxy_url,
                                ("https://mobile.squirt.org/",),
                            )
                            if not browser.get("challenge") and 200 <= browser.get("status", 0) < 300 and len(browser.get("body", "").encode("utf-8")) <= MAX_RESPONSE_BYTES:
                                status = browser["status"]
                                final_url = browser["final_url"]
                                headers = browser["headers"]
                                body = browser["body"]
                                error = browser.get("error")
                            else:
                                blocked = bool(browser.get("challenge"))
                                if blocked:
                                    reason = "challenge persisted after headed Chromium and stealth retry"
                                    if browser.get("proxy_required"):
                                        reason += "; PROXY_URL is unset, so the proxy rung could not be attempted"
                                    _append_gap(reason, current)
                                    failures.append("challenge_unresolved")
                                else:
                                    failures.append(browser.get("error") or f"browser_http_{browser.get('status', 0)}")
                        else:
                            failures.append("source_time_cap_reached")
                    _store_cache(database, url, iso_utc(current), status, final_url, headers, body, error)
                    cached[url] = _cache_row(database, url, current)
                    if status == 429 or status == 403 or headers.get("cf-mitigated"):
                        blocked = True
                        failures.append(f"http_{status}_challenge")
                        _append_gap(f"HTTP {status} challenge remains; response cached and next attempt deferred by the 30-minute source floor", current)
                        break
                    if not 200 <= status < 300:
                        failures.append(f"http_{status}")
                    if error:
                        failures.append(error)
                except httpx.RequestError as error:
                    summary = f"request_error_{type(error).__name__}"
                    _store_cache(database, url, iso_utc(current), 0, url, {}, "", summary)
                    cached[url] = _cache_row(database, url, current)
                    if time.monotonic() < deadline:
                        browser = fetch_with_browser(url, PROFILE_DIR, deadline, proxy_url, ("https://mobile.squirt.org/",))
                        if not browser.get("challenge") and 200 <= browser.get("status", 0) < 300:
                            body = browser.get("body", "")
                            if len(body.encode("utf-8")) <= MAX_RESPONSE_BYTES:
                                _store_cache(database, url, iso_utc(current), browser["status"], browser["final_url"], browser["headers"], body, browser.get("error"))
                                cached[url] = _cache_row(database, url, current)
                            else:
                                failures.append("response_too_large")
                        else:
                            blocked = bool(browser.get("challenge"))
                            if blocked:
                                reason = "challenge persisted after headed Chromium and stealth retry"
                                if browser.get("proxy_required"):
                                    reason += "; PROXY_URL is unset, so the proxy rung could not be attempted"
                                _append_gap(reason, current)
                            failures.append(browser.get("error") or summary)
                    else:
                        failures.append(summary)
        listings = [] if probe_only else [item for url in urls if cached.get(url) and cached[url][1] == 200 for item in parse_listings(cached[url][4], cached[url][2], url)]
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        added = 0 if dry or probe_only else _persist_listings(database, listings, iso_utc(current))
        successful = sum(1 for url in pending if cached.get(url) and 200 <= cached[url][1] < 300)
        status = "blocked" if blocked and successful == 0 else "partial" if failures and successful else "failed" if failures else "succeeded"
        database.execute(
            "UPDATE runs SET finished_at = ?, status = ?, listings_seen = ?, listings_added = ?, error_summary = ? WHERE id = ?",
            (iso_utc(current), status, len(listings), added, ", ".join(dict.fromkeys(failures)) or None, run_id),
        )
        database.commit()
    finally:
        database.close()
    notice = "Homepage connectivity probe only; no listings were parsed." if probe_only else "; ".join(dict.fromkeys(failures)) if failures else None
    return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": notice}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Fetch and cache, but do not save listing rows")
    parser.add_argument("--probe-homepage", action="store_true", help="Check the public homepage only when no listings URL is configured")
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    try:
        result = collect(profile_path=args.profile, db_path=args.db, dry=args.dry, probe_homepage=args.probe_homepage)
    except (OSError, ValueError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"Squirt run failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"count: {result['count']}")
    if result.get("notice"):
        print(f"notice: {result['notice']}")
    if args.dry:
        print("dry: listing rows not saved; URL cache and run state remain active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
