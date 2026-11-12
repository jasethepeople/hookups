from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import yaml
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from selectolax.lexbor import LexborHTMLParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import DEFAULT_DB_PATH, migrate

SOURCE = "doublelist"
BASE_URL = "https://doublelist.com"
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
GAPS_PATH = PROJECT_ROOT / "docs" / "GAPS.md"
PROFILE_DIR = PROJECT_ROOT / "cache" / "doublelist-profile"
CACHE_TTL = timedelta(minutes=30)
POLL_FLOOR = timedelta(minutes=30)
SOURCE_CAP_SECONDS = 600
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
NAVIGATION_TIMEOUT_MS = 25000
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
CARD_SELECTOR = "article, [data-post-id], [data-ad-id], [data-testid*='post'], [class*='post-card'], [class*='PostCard'], [class*='listing-card'], [class*='postItem']"
CHALLENGE_MARKERS = (
    "cf-chl-",
    "challenge-platform",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "access denied",
    "captcha",
)
STEALTH_SCRIPT = """Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }); Object.defineProperty(Navigator.prototype, 'languages', { get: () => ['en-US', 'en'] }); Object.defineProperty(Navigator.prototype, 'plugins', { get: () => [1, 2, 3, 4, 5] });"""
FALLBACK_PATHS = ("/s/personals", "/choose_city")


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
    config = profile.get(SOURCE) or {}
    if not isinstance(config, dict):
        raise ValueError("profile.doublelist must be a mapping")
    city_slug = config.get("city_slug")
    categories = config.get("categories") or []
    if not city_slug or not categories:
        return []
    if not isinstance(city_slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", city_slug.strip()):
        raise ValueError("doublelist.city_slug must be one city path slug")
    if not isinstance(categories, list):
        raise ValueError("doublelist.categories must be a list of category slugs")
    city = quote(city_slug.strip(), safe="-_")
    urls = []
    for category in categories:
        if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", category.strip()):
            raise ValueError(f"Invalid Doublelist category slug: {category!r}")
        urls.append(f"{BASE_URL}/city/{city}/{quote(category.strip(), safe='-_')}")
    return list(dict.fromkeys(urls))


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/") or "/", urlencode(query), ""))


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


def absolute_doublelist_url(base_url: str, href: str | None) -> str | None:
    if not href or href.strip().lower().startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = canonicalize_url(urljoin(base_url, href.strip()))
    host = (urlsplit(absolute).hostname or "").lower()
    if host != "doublelist.com" and not host.endswith(".doublelist.com"):
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
        "a[href*='/post/']",
        "a[href*='/ad/']",
        "a[href]",
    ):
        anchors.extend(node.css(selector))
    seen = set()
    for anchor in anchors:
        url = absolute_doublelist_url(base_url, anchor.attrs.get("href"))
        if not url or url == search_url or url in seen:
            continue
        seen.add(url)
        path = urlsplit(url).path.lower()
        if any(part in path for part in ("/login", "/signup", "/register", "/choose_city", "/category/", "/s/", "/city/")):
            continue
        return anchor, url
    return None, None


def parse_listings(html: str, base_url: str, search_url: str | None = None) -> list[dict]:
    base = canonicalize_url(base_url)
    search = canonicalize_url(search_url or base_url)
    parser = LexborHTMLParser(html)
    candidates = parser.css(CARD_SELECTOR)
    if not candidates:
        candidates = parser.css("h2 a[href], h3 a[href], a[href*='/post/'], a[href*='/ad/']")
    unique: dict[str, dict] = {}
    for card in candidates:
        anchor, url = listing_anchor(card, base, search)
        if not url:
            continue
        title = first_value(card, ("h2", "h3", "[class*='title']")) or text_of(anchor)
        if not title:
            continue
        price = first_value(card, ("[itemprop='price']", "[class*='price']", "[data-price]"))
        location = first_value(card, ("[itemprop='addressLocality']", "address", "[class*='location']", "[data-testid*='location']"))
        posted = first_value(card, ("time[datetime]", "[data-posted]", "[class*='posted']", "[class*='date']", "[class*='time']"))
        image = None
        for image_node in card.css("img[data-src], img[data-original], img[src]"):
            raw_image = image_node.attrs.get("data-src") or image_node.attrs.get("data-original") or image_node.attrs.get("src")
            if raw_image and not raw_image.strip().lower().startswith("data:"):
                image = urljoin(base, raw_image.strip())
                break
        source_id = card.attrs.get("data-post-id") or card.attrs.get("data-ad-id") or card.attrs.get("data-id")
        if not source_id:
            match = re.search(r"(?:^|/)(?:post|ad)/([A-Za-z0-9_-]+)(?:/|$)", urlsplit(url).path, re.IGNORECASE)
            if match:
                source_id = match.group(1)
        item = {
            "source": SOURCE,
            "source_id": source_id.strip() if source_id else None,
            "dedupe_key": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "title": title[:500],
            "url": url,
            "price": price,
            "location": location,
            "posted": posted,
            "thumb": image,
            "text_excerpt": text_of(card)[:1000],
        }
        if url not in unique:
            unique[url] = item
        else:
            for key, value in item.items():
                if unique[url].get(key) in (None, "") and value not in (None, ""):
                    unique[url][key] = value
    return list(unique.values())


def _safe_headers(headers) -> dict:
    return {name: headers.get(name, "") for name in SAFE_RESPONSE_HEADERS if headers.get(name) is not None}


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


def _store_cache(connection: sqlite3.Connection, result: dict, fetched_at: str) -> None:
    url = result["requested_url"]
    connection.execute(
        "INSERT INTO http_cache (source, url, fetched_at, status_code, final_url, headers_json, body, error_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, url) DO UPDATE SET fetched_at = excluded.fetched_at, status_code = excluded.status_code, final_url = excluded.final_url, headers_json = excluded.headers_json, body = excluded.body, error_summary = excluded.error_summary",
        (SOURCE, url, fetched_at, result["status_code"], result["final_url"], json.dumps(result["headers"], sort_keys=True), result["body"], result["error"]),
    )


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
    changed = 0
    for item in listings:
        cursor = connection.execute(
            "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, price_text, text_excerpt, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status, thumb_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', 'new', ?) ON CONFLICT(source, dedupe_key) DO UPDATE SET source_id = COALESCE(excluded.source_id, listings.source_id), canonical_url = excluded.canonical_url, title = excluded.title, price_text = COALESCE(excluded.price_text, listings.price_text), text_excerpt = excluded.text_excerpt, posted_at = COALESCE(excluded.posted_at, listings.posted_at), last_seen_at = excluded.last_seen_at, locality = COALESCE(excluded.locality, listings.locality), thumb_url = COALESCE(excluded.thumb_url, listings.thumb_url)",
            (SOURCE, item["dedupe_key"], item.get("source_id"), item["url"], item["title"], item.get("price"), item.get("text_excerpt", ""), item.get("posted"), now_text, now_text, item.get("location"), item.get("thumb")),
        )
        changed += max(cursor.rowcount, 0)
    connection.commit()
    return changed


def _append_gap(message: str, now: datetime) -> None:
    line = f"- {now.date().isoformat()} Doublelist collection: {message}."
    try:
        current = GAPS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# Gaps\n"
    if line not in current:
        with GAPS_PATH.open("a", encoding="utf-8") as file:
            if current and not current.endswith("\n"):
                file.write("\n")
            file.write(line + "\n")


def _is_challenge(page, status_code: int) -> bool:
    if status_code in (403, 429, 503):
        return True
    try:
        body = page.content().lower()
        title = page.title().lower()
    except Exception:
        return True
    return any(marker in body or marker in title for marker in CHALLENGE_MARKERS)


def _capture(page, requested_url: str, response, error: str | None = None) -> dict:
    status_code = response.status if response is not None else 0
    headers = _safe_headers(response.headers) if response is not None else {}
    final_url = canonicalize_url(page.url) if page.url and page.url != "about:blank" else requested_url
    try:
        body = page.content()
    except Exception:
        body = ""
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        body = ""
        error = "response_too_large"
    challenge = _is_challenge(page, status_code)
    return {
        "requested_url": canonicalize_url(requested_url),
        "status_code": status_code,
        "final_url": final_url,
        "headers": headers,
        "body": body,
        "error": error,
        "challenged": challenge,
    }


def _navigate(page, url: str, deadline: float) -> dict:
    remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
    response = None
    error = None
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=min(NAVIGATION_TIMEOUT_MS, remaining_ms))
        if time.monotonic() < deadline:
            page.wait_for_timeout(min(1000, max(0, int((deadline - time.monotonic()) * 1000))))
    except PlaywrightTimeoutError:
        error = "navigation_timeout"
    except Exception as exception:
        error = f"browser_error_{type(exception).__name__}"
    return _capture(page, url, response, error)


def _wait_challenge(page, status_code: int, deadline: float, seconds: int = 25) -> bool:
    wait_until = min(deadline, time.monotonic() + seconds)
    while time.monotonic() < wait_until:
        if not _is_challenge(page, status_code):
            return False
        page.wait_for_timeout(1000)
        status_code = 200 if any(cookie["name"] == "cf_clearance" for cookie in page.context.cookies()) else status_code
    return _is_challenge(page, status_code)


def _visit_with_ladder(context, url: str, deadline: float) -> tuple[list[dict], object, bool]:
    page = context.new_page()
    page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    result = _navigate(page, url, deadline)
    attempts = [result]
    if result["error"] and time.monotonic() < deadline:
        page.wait_for_timeout(min(1500, max(0, int((deadline - time.monotonic()) * 1000))))
        result = _navigate(page, url, deadline)
        attempts.append(result)
    if not result["challenged"]:
        return attempts, page, False
    _wait_challenge(page, result["status_code"], deadline, 30)
    if time.monotonic() < deadline:
        page.add_init_script(STEALTH_SCRIPT)
        result = _navigate(page, url, deadline)
        attempts.append(result)
    if result["challenged"] and time.monotonic() < deadline:
        _wait_challenge(page, result["status_code"], deadline, 30)
        if time.monotonic() < deadline:
            page.set_viewport_size({"width": 390, "height": 844})
            result = _navigate(page, url, deadline)
            attempts.append(result)
    return attempts, page, result["challenged"]


def _launch_context(playwright, proxy_url: str | None):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PROFILE_DIR, 0o700)
    options = {
        "headless": False,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        "locale": "en-US",
        "timezone_id": "America/Denver",
        "viewport": {"width": 1365, "height": 900},
        "accept_downloads": False,
    }
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
    context = playwright.chromium.launch_persistent_context(str(PROFILE_DIR), **options)
    context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    return context


def _browser_fetch(url: str, deadline: float, proxy_url: str | None) -> tuple[list[dict], bool, bool]:
    all_attempts = []
    proxy_used = False
    with sync_playwright() as playwright:
        context = _launch_context(playwright, None)
        try:
            attempts, page, blocked = _visit_with_ladder(context, url, deadline)
            all_attempts.extend(attempts)
            if blocked and proxy_url and time.monotonic() < deadline:
                context.close()
                context = _launch_context(playwright, proxy_url)
                proxy_used = True
                attempts, page, blocked = _visit_with_ladder(context, url, deadline)
                all_attempts.extend(attempts)
            if blocked and time.monotonic() < deadline:
                for path in FALLBACK_PATHS:
                    fallback_url = urljoin(BASE_URL, path)
                    attempts, page, fallback_blocked = _visit_with_ladder(context, fallback_url, deadline)
                    all_attempts.extend(attempts)
                    if not fallback_blocked and fallback_url != url:
                        attempts, page, blocked = _visit_with_ladder(context, url, deadline)
                        all_attempts.extend(attempts)
                        blocked = blocked
                        break
                    blocked = fallback_blocked
                    if time.monotonic() >= deadline:
                        break
            return all_attempts, blocked, proxy_used
        finally:
            context.close()


def _result_from_cache(row: tuple, url: str) -> dict:
    fetched_at, status_code, final_url, headers_json, body, error = row
    challenge = status_code in (403, 429, 503) or "challenge" in (error or "")
    return {
        "requested_url": url,
        "status_code": status_code,
        "final_url": final_url,
        "headers": json.loads(headers_json or "{}"),
        "body": body,
        "error": error,
        "challenged": challenge,
    }


def _cache_listing_rows(database: sqlite3.Connection, urls: list[str], now: datetime) -> list[dict]:
    listings = []
    for url in urls:
        row = _cache_row(database, url, now)
        if row:
            listings.extend(_parse_cached(row, url))
    return list({item["url"]: item for item in listings}.values())


def _cache_is_challenge(row: tuple | None) -> bool:
    if not row:
        return False
    _, status_code, _, headers_json, body, error = row
    if status_code in (403, 429, 503) or "challenge" in (error or "").lower():
        return True
    headers = json.loads(headers_json or "{}")
    if headers.get("cf-mitigated"):
        return True
    content = (body or "")[:12000].lower()
    return any(marker in content for marker in CHALLENGE_MARKERS)


def _configured_proxy() -> str | None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return os.environ.get("PROXY_URL", "").strip() or None


def collect(
    profile_path: str | Path = PROFILE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    dry: bool = False,
    probe_homepage: bool = False,
    now: datetime | None = None,
) -> dict:
    current = utc_now(now)
    profile = load_profile(profile_path)
    search_urls = build_search_urls(profile)
    urls = search_urls or ([canonicalize_url(BASE_URL)] if probe_homepage else [])
    migrate(db_path)
    database = sqlite3.connect(str(db_path), timeout=30)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("DELETE FROM http_cache WHERE fetched_at < ?", ((current - CACHE_TTL).isoformat(timespec="seconds"),))
    database.commit()
    proxy_url = _configured_proxy()
    if not urls:
        database.close()
        return {"count": 0, "first_3": [], "listings": [], "notice": "Set doublelist.city_slug and doublelist.categories in config/profile.yaml; no request was made.", "challenge": False, "proxy_required": None, "residential_proxy_requirement": "unknown", "proxy_used": False, "proxy_configured": bool(proxy_url)}
    cached = {url: _cache_row(database, url, current) for url in urls}
    pending = [url for url in urls if cached[url] is None]
    last_start = _last_source_start(database)
    if pending and last_start and current - last_start < POLL_FLOOR:
        listings = _cache_listing_rows(database, urls, current) if search_urls else []
        blocked = any(_cache_is_challenge(cached[url]) for url in urls)
        database.close()
        wait_until = (last_start + POLL_FLOOR).isoformat(timespec="seconds")
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": f"Source poll floor active until {wait_until}; no network request was made.", "challenge": blocked, "proxy_required": True if blocked and not proxy_url else None, "residential_proxy_requirement": "unverified" if blocked else "unknown", "proxy_used": False, "proxy_configured": bool(proxy_url)}
    if not pending:
        listings = _cache_listing_rows(database, urls, current) if search_urls else []
        blocked = any(_cache_is_challenge(cached[url]) for url in urls)
        database.close()
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": "Fresh response cache used; no network request was made.", "challenge": blocked, "proxy_required": True if blocked and not proxy_url else False, "residential_proxy_requirement": "unverified" if blocked else "no", "proxy_used": False, "proxy_configured": bool(proxy_url)}
    run_cursor = database.execute(
        "INSERT INTO runs (source, started_at, finished_at, status, listings_seen, listings_added, listings_updated, error_summary) VALUES (?, ?, NULL, 'running', 0, 0, 0, NULL)",
        (SOURCE, iso_utc(current)),
    )
    run_id = run_cursor.lastrowid
    database.commit()
    deadline = time.monotonic() + SOURCE_CAP_SECONDS
    attempts_by_url = {}
    unresolved = False
    proxy_used = False
    failures = []
    for url in pending:
        try:
            attempts, blocked, used_proxy = _browser_fetch(url, deadline, proxy_url)
        except Exception as error:
            attempts = [{"requested_url": url, "status_code": 0, "final_url": url, "headers": {}, "body": "", "error": f"browser_error_{type(error).__name__}", "challenged": False}]
            blocked = False
            used_proxy = False
        attempts_by_url[url] = attempts
        proxy_used = proxy_used or used_proxy
        for attempt in attempts:
            _store_cache(database, attempt, iso_utc(current))
            if attempt.get("error"):
                failures.append(attempt["error"])
            if attempt["status_code"] and not 200 <= attempt["status_code"] < 300 and not attempt["challenged"]:
                failures.append(f"http_{attempt['status_code']}")
        database.commit()
        if blocked:
            unresolved = True
            failures.append("challenge_not_cleared")
            break
    for url in urls:
        if cached.get(url) is None and url in attempts_by_url and attempts_by_url[url]:
            latest = attempts_by_url[url][-1]
            cached[url] = (
                iso_utc(current),
                latest["status_code"],
                latest["final_url"],
                json.dumps(latest["headers"], sort_keys=True),
                latest["body"],
                latest["error"],
            )
    listings = _cache_listing_rows(database, search_urls, current) if search_urls else []
    added = 0 if dry else _persist_listings(database, listings, iso_utc(current))
    proxy_required = unresolved and not proxy_url
    residential_proxy_requirement = "unverified" if unresolved else "no"
    if unresolved:
        next_rung = "required" if proxy_required else "not required"
        _append_gap(
            f"challenge remained after headed persistent Chromium, wait, stealth retry, mobile layout, and official fallback pages; PROXY_URL for the next proxy rung={next_rung}; residential proxy type unverified; no RSS feed or official mirror was identified",
            current,
        )
    successful = sum(1 for url in pending if url in attempts_by_url and attempts_by_url[url] and 200 <= attempts_by_url[url][-1]["status_code"] < 300 and not attempts_by_url[url][-1]["challenged"])
    if unresolved and successful == 0:
        status = "blocked"
    elif failures and successful:
        status = "partial"
    elif failures:
        status = "failed"
    else:
        status = "succeeded"
    database.execute(
        "UPDATE runs SET finished_at = ?, status = ?, listings_seen = ?, listings_added = ?, error_summary = ? WHERE id = ?",
        (iso_utc(current), status, len(listings), added, ", ".join(failures) or None, run_id),
    )
    database.commit()
    database.close()
    notice = None
    if not search_urls:
        notice = "Homepage probe only; no local city/category was configured, so no listing URLs were requested."
    if unresolved:
        notice = "Challenge could not be cleared; see docs/GAPS.md."
    elif proxy_used:
        notice = "A configured proxy was used successfully; residential status was not independently verified."
    return {
        "count": len(listings),
        "first_3": listings[:3],
        "listings": listings,
        "notice": notice,
        "challenge": unresolved,
        "proxy_required": proxy_required,
        "residential_proxy_requirement": residential_proxy_requirement,
        "proxy_used": proxy_used,
        "proxy_configured": bool(proxy_url),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Fetch and cache, print results, but do not save listing rows")
    parser.add_argument("--probe-homepage", action="store_true", help="Check site access without needing profile search settings")
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--_xvfb-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args._xvfb_worker and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--_xvfb-worker"]
        try:
            return subprocess.run(command, timeout=SOURCE_CAP_SECONDS + 30, check=False).returncode
        except subprocess.TimeoutExpired:
            print("Doublelist run exceeded the 10-minute source cap", file=sys.stderr)
            return 124
    try:
        result = collect(profile_path=args.profile, db_path=args.db, dry=args.dry, probe_homepage=args.probe_homepage)
    except (OSError, ValueError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"Doublelist run failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"count: {result['count']}")
    print("first 3:")
    print(json.dumps(result["first_3"], indent=2, ensure_ascii=False))
    if result.get("notice"):
        print(f"notice: {result['notice']}")
    print(f"challenge: {'yes' if result.get('challenge') else 'no'}")
    if result.get("proxy_required") is None:
        print("proxy_key_required_for_next_rung: unknown")
    else:
        print(f"proxy_key_required_for_next_rung: {'yes' if result['proxy_required'] else 'no'}")
    print(f"residential_proxy_requirement: {result.get('residential_proxy_requirement', 'unknown')}")
    if args.dry:
        print("dry: listing rows not saved; URL cache, persistent browser state, and run state remain active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
