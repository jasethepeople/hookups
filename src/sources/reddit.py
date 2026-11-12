from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.lexbor import LexborHTMLParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import DEFAULT_DB_PATH, migrate
from src.sources.browser import fetch_with_browser, is_challenge

GAPS_PATH = PROJECT_ROOT / "docs" / "GAPS.md"
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
PROFILE_DIR = PROJECT_ROOT / "cache" / "reddit-profile"
SOURCE = "reddit"
SUBREDDITS = ("SLCr4r", "r4r")
BASE_URL = "https://www.reddit.com"
CACHE_TTL = timedelta(minutes=30)
POLL_FLOOR = timedelta(minutes=30)
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json,text/plain,*/*",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control", "cf-mitigated", "content-length", "content-type", "date",
    "etag", "last-modified", "location", "retry-after", "server",
}
R4R_GENDER_TAG = re.compile(r"\b[A-Z0-9]{1,8}4[A-Z0-9]{1,8}\b", re.I)


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


def build_json_urls(subreddits: tuple[str, ...] = SUBREDDITS) -> list[str]:
    if not isinstance(subreddits, (list, tuple)) or not subreddits:
        raise ValueError("reddit.subreddits must be a non-empty list")
    urls = []
    for subreddit in subreddits:
        if not isinstance(subreddit, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            raise ValueError(f"Invalid subreddit name: {subreddit!r}")
        query = urlencode({"limit": "100", "raw_json": "1"})
        urls.append(f"{BASE_URL}/r/{subreddit}/new.json?{query}")
    return urls


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def _r4r_tagged(post: dict, subreddit: str) -> bool:
    if subreddit.lower() == "r4r":
        return True
    flair = str(post.get("link_flair_text") or "")
    title = str(post.get("title") or "")
    flair_text = flair.strip().strip("[]").upper()
    if flair_text == "R4R" or R4R_GENDER_TAG.search(flair_text):
        return True
    for bracket in re.findall(r"\[([^\]]{1,80})\]", title):
        label = bracket.strip().upper()
        if label == "R4R" or R4R_GENDER_TAG.search(label):
            return True
    return False


def _post_url(post: dict) -> str | None:
    permalink = post.get("permalink")
    if not isinstance(permalink, str) or not permalink.strip():
        return None
    if permalink.startswith("http://") or permalink.startswith("https://"):
        parts = urlsplit(permalink)
        if (parts.hostname or "").lower() not in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
            return None
        return canonicalize_url(permalink)
    if not permalink.startswith("/"):
        return None
    return canonicalize_url(f"{BASE_URL}{permalink}")


def _clean_post(post: dict, requested_subreddit: str) -> dict | None:
    subreddit = str(post.get("subreddit") or requested_subreddit)
    if not _r4r_tagged(post, subreddit):
        return None
    post_id = str(post.get("id") or "").strip()
    url = _post_url(post)
    title = " ".join(str(post.get("title") or "").split())
    if not post_id or not url or not title:
        return None
    created = post.get("created_utc")
    try:
        posted = datetime.fromtimestamp(float(created), timezone.utc).isoformat(timespec="seconds") if created is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        posted = None
    thumbnail = str(post.get("thumbnail") or "").strip()
    if thumbnail.lower() in {"", "self", "default", "nsfw", "spoiler", "image", ""} or not thumbnail.startswith(("https://", "http://")):
        thumbnail = None
    excerpt = " ".join(str(post.get("selftext") or "").split())[:1000]
    return {
        "source": SOURCE,
        "source_id": post_id,
        "dedupe_key": f"{subreddit.lower()}:{post_id}",
        "title": title[:500],
        "url": url,
        "price": None,
        "location": None,
        "posted": posted,
        "thumb": html.unescape(thumbnail) if thumbnail else None,
        "text_excerpt": excerpt,
    }


def _minimal_payload(payload: dict, requested_subreddit: str) -> tuple[str, int, int]:
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise ValueError("Reddit response does not contain a data.children list")
    kept = []
    rejected = 0
    for child in children:
        post = child.get("data") if isinstance(child, dict) else None
        if not isinstance(post, dict):
            continue
        cleaned = _clean_post(post, requested_subreddit)
        if cleaned is None:
            rejected += 1
            continue
        kept.append({
            "id": cleaned["source_id"],
            "subreddit": str(post.get("subreddit") or requested_subreddit),
            "permalink": cleaned["url"],
            "title": cleaned["title"],
            "created_utc": post.get("created_utc"),
            "selftext": cleaned["text_excerpt"],
            "thumbnail": cleaned["thumb"],
            "link_flair_text": "R4R",
        })
    serialized = json.dumps({"data": {"children": [{"kind": "t3", "data": post} for post in kept]}}, ensure_ascii=False, separators=(",", ":"))
    return serialized, len(kept), rejected


def parse_listings(body: str, requested_subreddit: str) -> list[dict]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        try:
            parser = LexborHTMLParser(body)
            pre = parser.css_first("pre")
            payload = json.loads(pre.text(separator="", strip=True) if pre else "")
        except (ValueError, TypeError):
            return []
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        return []
    results = {}
    for child in children:
        post = child.get("data") if isinstance(child, dict) else None
        if not isinstance(post, dict):
            continue
        item = _clean_post(post, requested_subreddit)
        if item:
            results[item["url"]] = item
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


def _last_source_run(connection: sqlite3.Connection):
    return connection.execute(
        "SELECT id, started_at, status, error_summary FROM runs WHERE source = ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (SOURCE,),
    ).fetchone()


def _response_headers(headers) -> dict:
    return {key: headers.get(key, "") for key in SAFE_RESPONSE_HEADERS if headers.get(key) is not None}


def _append_gap(message: str, now: datetime) -> None:
    line = f"- {now.date().isoformat()} Reddit collection: {message}."
    try:
        current = GAPS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# Gaps\n"
    if line not in current:
        with GAPS_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")


def _browser_json(url: str, subreddit: str, deadline: float, proxy_url: str | None) -> dict:
    fallback = url.replace("https://www.reddit.com/", "https://old.reddit.com/", 1)
    browser = fetch_with_browser(url, PROFILE_DIR, deadline, proxy_url, (fallback,))
    raw = browser.get("text", "").strip()
    body = raw
    if not raw.startswith("{"):
        html_body = browser.get("body", "")
        if html_body:
            parser = LexborHTMLParser(html_body)
            pre = parser.css_first("pre")
            body = pre.text(separator="", strip=True) if pre else ""
    try:
        payload = json.loads(body)
        serialized, kept, rejected = _minimal_payload(payload, subreddit)
        browser["body"] = serialized
        browser["kept"] = kept
        browser["rejected"] = rejected
        return browser
    except (json.JSONDecodeError, ValueError, TypeError):
        browser["body"] = ""
        browser["error"] = browser.get("error") or "browser_response_not_json"
        return browser


def collect(
    profile_path: str | Path = PROFILE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    dry: bool = False,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict:
    current = utc_now(now)
    profile = load_profile(profile_path)
    config = profile.get("reddit") or {}
    if not isinstance(config, dict):
        raise ValueError("profile.reddit must be a mapping")
    subreddits = config.get("subreddits", SUBREDDITS)
    urls = build_json_urls(subreddits)
    url_subreddit = {url: subreddit for url, subreddit in zip(urls, subreddits)}
    migrate(db_path)
    database = sqlite3.connect(str(db_path), timeout=30)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("DELETE FROM http_cache WHERE fetched_at < ?", ((current - CACHE_TTL).isoformat(timespec="seconds"),))
    database.commit()
    cached = {url: _cache_row(database, url, current) for url in urls}
    pending = [url for url in urls if cached[url] is None]
    last_run = _last_source_run(database)
    last_start = parse_timestamp(last_run[1]) if last_run else None
    elapsed = (current - last_start).total_seconds() if last_start else None
    resume_incomplete = bool(last_run and elapsed is not None and elapsed < 600 and last_run[2] in {"blocked", "partial"})
    if pending and last_start and current - last_start < POLL_FLOOR and not resume_incomplete:
        listings = [item for url in urls if cached[url] and cached[url][1] == 200 for item in parse_listings(cached[url][4], url_subreddit[url])]
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        if not dry:
            _persist_listings(database, listings, iso_utc(current))
        wait_until = (last_start + POLL_FLOOR).isoformat(timespec="seconds")
        database.close()
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": f"Source poll floor active until {wait_until}; no network request was made.", "rejected_by_r4r_tag": None, "proxy_required": None, "residential_proxy_requirement": "unknown"}
    if not pending:
        listings = [item for url in urls if cached[url] and cached[url][1] == 200 for item in parse_listings(cached[url][4], url_subreddit[url])]
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        if not dry:
            _persist_listings(database, listings, iso_utc(current))
        database.close()
        return {"count": len(listings), "first_3": listings[:3], "listings": listings, "notice": "Fresh URL response cache used; no network request was made.", "rejected_by_r4r_tag": None, "proxy_required": False, "residential_proxy_requirement": "no"}
    if resume_incomplete:
        run_id = last_run[0]
        failures = [item.strip() for item in (last_run[3] or "").split(",") if item.strip()]
    else:
        cursor = database.execute(
            "INSERT INTO runs (source, started_at, finished_at, status, listings_seen, listings_added, listings_updated, error_summary) VALUES (?, ?, NULL, 'running', 0, 0, 0, NULL)",
            (SOURCE, iso_utc(current)),
        )
        run_id = cursor.lastrowid
        database.commit()
        failures = []
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None
    options = {"headers": REQUEST_HEADERS, "timeout": httpx.Timeout(20.0, connect=10.0), "follow_redirects": True, "max_redirects": 6}
    client_context = httpx.Client(**options) if client is None else nullcontext(client)
    blocked = any(cached.get(url) and (cached[url][1] in {403, 429, 503} or "challenge" in (cached[url][5] or "")) for url in urls)
    proxy_used = False
    rejected_by_tag = 0
    remaining = max(1.0, 600 - elapsed) if resume_incomplete and elapsed is not None else 600
    deadline = time.monotonic() + remaining
    try:
        with client_context as active_client:
            for url in pending:
                if time.monotonic() >= deadline:
                    failures.append("source_time_cap_reached")
                    break
                subreddit = url_subreddit[url]
                try:
                    response = active_client.get(url)
                    headers = _response_headers(response.headers)
                    status = response.status_code
                    final_url = str(response.url)
                    error = None
                    body = ""
                    kept = rejected = 0
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        error = "response_too_large"
                    elif 200 <= status < 300:
                        try:
                            serialized, kept, rejected = _minimal_payload(response.json(), subreddit)
                            body = serialized
                            rejected_by_tag += rejected
                        except (json.JSONDecodeError, ValueError, TypeError):
                            error = "invalid_json_response"
                    challenged = is_challenge(status, "", response.text[:20000], headers)
                    if challenged or error == "invalid_json_response":
                        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                        browser = _browser_json(url, subreddit, deadline, proxy_url)
                        proxy_used = proxy_used or bool(browser.get("proxy_used"))
                        if not browser.get("challenge") and 200 <= browser.get("status", 0) < 300 and browser.get("body"):
                            status = browser["status"]
                            final_url = browser["final_url"]
                            headers = browser["headers"]
                            body = browser["body"]
                            error = None
                            rejected_by_tag += int(browser.get("rejected", 0))
                        else:
                            blocked = bool(browser.get("challenge"))
                            error = browser.get("error") or ("challenge_unresolved" if blocked else f"http_{browser.get('status', 0)}")
                            if blocked:
                                message = f"HTTP {status} challenge on r/{subreddit} persisted after headed Chromium and stealth retry"
                                if browser.get("proxy_required"):
                                    message += "; PROXY_URL is unset, so the proxy rung could not be attempted"
                                _append_gap(message, current)
                            failures.append(error)
                    _store_cache(database, url, iso_utc(current), status, final_url, headers, body, error)
                    cached[url] = _cache_row(database, url, current)
                    if status in {403, 429, 503} or headers.get("cf-mitigated"):
                        blocked = True
                        failures.append(f"http_{status}_challenge")
                    if not 200 <= status < 300:
                        failures.append(f"http_{status}")
                    if error:
                        failures.append(error)
                except httpx.RequestError as error:
                    summary = f"request_error_{type(error).__name__}"
                    browser = _browser_json(url, subreddit, deadline, proxy_url)
                    proxy_used = proxy_used or bool(browser.get("proxy_used"))
                    blocked = bool(browser.get("challenge"))
                    if not blocked and 200 <= browser.get("status", 0) < 300 and browser.get("body"):
                        _store_cache(database, url, iso_utc(current), browser["status"], browser["final_url"], browser["headers"], browser["body"], None)
                        cached[url] = _cache_row(database, url, current)
                        rejected_by_tag += int(browser.get("rejected", 0))
                    else:
                        summary = browser.get("error") or summary
                        if blocked:
                            message = f"challenge on r/{subreddit} persisted after headed Chromium and stealth retry"
                            if browser.get("proxy_required"):
                                message += "; PROXY_URL is unset, so the proxy rung could not be attempted"
                            _append_gap(message, current)
                        failures.append(summary)
                        _store_cache(database, url, iso_utc(current), 0, url, {}, "", summary)
                        cached[url] = _cache_row(database, url, current)
        listings = [item for url in urls if cached.get(url) and cached[url][1] == 200 for item in parse_listings(cached[url][4], url_subreddit[url])]
        unique = {item["url"]: item for item in listings}
        listings = list(unique.values())
        added = 0 if dry else _persist_listings(database, listings, iso_utc(current))
        successful = sum(1 for url in pending if cached.get(url) and 200 <= cached[url][1] < 300)
        status = "blocked" if blocked and successful == 0 else "partial" if failures and successful else "failed" if failures else "succeeded"
        database.execute(
            "UPDATE runs SET finished_at = ?, status = ?, listings_seen = ?, listings_added = ?, error_summary = ? WHERE id = ?",
            (iso_utc(current), status, len(listings), added, ", ".join(dict.fromkeys(failures)) or None, run_id),
        )
        database.commit()
    finally:
        database.close()
    return {
        "count": len(listings),
        "first_3": listings[:3],
        "listings": listings,
        "rejected_by_r4r_tag": rejected_by_tag,
        "notice": "; ".join(dict.fromkeys(failures)) or None,
        "challenge": blocked,
        "proxy_used": proxy_used,
        "proxy_required": bool(blocked and not proxy_url),
        "residential_proxy_requirement": "unverified" if blocked and not proxy_url else "not-required" if not blocked else "unverified",
    }


def _persist_listings(connection: sqlite3.Connection, listings: list[dict], now_text: str) -> int:
    changed = 0
    for item in listings:
        cursor = connection.execute(
            "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, price_text, text_excerpt, thumb_url, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, 'new') ON CONFLICT(source, dedupe_key) DO UPDATE SET source_id = COALESCE(excluded.source_id, listings.source_id), canonical_url = excluded.canonical_url, title = excluded.title, text_excerpt = excluded.text_excerpt, thumb_url = COALESCE(excluded.thumb_url, listings.thumb_url), posted_at = COALESCE(excluded.posted_at, listings.posted_at), last_seen_at = excluded.last_seen_at, locality = COALESCE(excluded.locality, listings.locality), tags_json = excluded.tags_json",
            (SOURCE, item["dedupe_key"], item.get("source_id"), item["url"], item["title"], item.get("text_excerpt", ""), item.get("thumb"), item.get("posted"), now_text, now_text, item.get("location"), json.dumps(item.get("tags", []))),
        )
        changed += max(cursor.rowcount, 0)
    connection.commit()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Fetch and cache, but do not save listing rows")
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    try:
        result = collect(profile_path=args.profile, db_path=args.db, dry=args.dry)
    except (OSError, ValueError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"Reddit run failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"count: {result['count']}")
    if result.get("rejected_by_r4r_tag") is not None:
        print(f"rejected by R4R-tag filter: {result['rejected_by_r4r_tag']}")
    if result.get("notice"):
        print(f"notice: {result['notice']}")
    if result.get("challenge"):
        print("proxy key required for next rung: yes" if result.get("proxy_required") else "proxy key required for next rung: no; configured proxy was used")
        print(f"residential proxy requirement: {result['residential_proxy_requirement']}")
    if args.dry:
        print("dry: listing rows not saved; URL cache and run state remain active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
