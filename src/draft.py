from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.db import DEFAULT_DB_PATH, migrate
from src.dedupe import dedupe_records
from src.normalize import normalize_record
from src.score import load_profile, score_records

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"


def _clean_text(value: object, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def _hook(record: dict[str, Any]) -> str:
    value = _clean_text(record.get("title"), 180)
    if not value:
        value = _clean_text(record.get("body"), 180).split(". ", 1)[0]
    value = re.sub(r"[.!?]+", "", value)
    words = value.split()
    return " ".join(words[:8]).strip(" ,;:-")


def _self_stats(profile: dict[str, Any]) -> list[str]:
    raw = profile.get("self_stats", {})
    if not isinstance(raw, dict):
        return []
    stats: list[str] = []
    age = raw.get("age")
    try:
        age_value = int(age) if age is not None else None
    except (TypeError, ValueError):
        age_value = None
    if age_value is not None and 18 <= age_value <= 120:
        stats.append(f"Age {age_value}")
    for key in ("body_type", "position", "location", "host_or_travel"):
        value = _clean_text(raw.get(key), 48)
        if value and value.casefold() not in {"null", "none", "unknown", "unspecified"}:
            stats.append(value)
    return stats


def _words(text: str) -> int:
    return len(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", text, flags=re.UNICODE))


def _grindr_variants(hook: str, stats: list[str]) -> tuple[str, str]:
    prefix_parts: list[str] = []
    for part in stats:
        candidate = ", ".join(prefix_parts + [part])
        if _words(candidate) <= 6:
            prefix_parts.append(part)
    prefix = ", ".join(prefix_parts)
    hook_words = hook.split()
    variants: list[str] = []
    for phrase in (
        f"“{{hook}}” caught my eye.",
        f"“{{hook}}”: open to chat?"
    ):
        chosen = ""
        for count in range(min(4, len(hook_words)), -1, -1):
            short_hook = " ".join(hook_words[:count])
            message = phrase.format(hook=short_hook)
            candidate = f"{prefix}: {message}" if prefix else message
            if _words(candidate) <= 10:
                chosen = candidate
                break
        variants.append(chosen)
    if not all(variants):
        raise ValueError("Could not produce a ten-word Grindr draft")
    return variants[0], variants[1]


def draft_variants(record: dict[str, Any], self_stats: list[str]) -> tuple[str, str]:
    hook = _hook(record)
    if not hook:
        raise ValueError("Listing has no title or body text to use as a hook")
    source = str(record.get("source") or "").casefold()
    quote = f"“{hook}”"
    if source in {"grindr", "grindr-via-notification-bridge"}:
        return _grindr_variants(hook, self_stats)
    if source == "doublelist":
        stats_sentence = f"My stats: {', '.join(self_stats)}." if self_stats else ""
        first = [f"Your post mentioned {quote}."]
        if stats_sentence:
            first.append(stats_sentence)
        first.extend(("Would you be open to chatting?", "What kind of timing works for you?"))
        second = [f"I noticed {quote} in your ad."]
        if stats_sentence:
            second.append(stats_sentence)
        second.extend(("Would you be open to a conversation?", "What are you looking for?"))
        return " ".join(first), " ".join(second)
    if source == "reddit":
        return (
            f"Your post mentions {quote}. Open to chatting?",
            f"Re: {quote} — what are you looking for?",
        )
    return (
        f"Your post mentions {quote}. Open to chatting?",
        f"Re: {quote} — what kind of timing works for you?",
    )


def _domain_is_blocked(url: str, blocked_domains: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in blocked_domains)


def _load_records(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    blocked_domains = {
        str(row[0]).casefold().removeprefix("www.")
        for row in connection.execute("SELECT domain FROM blocked_domains")
    }
    rows = connection.execute(
        "SELECT id, source, source_id, canonical_url, title, text_excerpt, thumb_url, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status FROM listings WHERE review_status NOT IN ('hidden', 'contacted')"
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        if _domain_is_blocked(str(row["canonical_url"] or ""), blocked_domains):
            continue
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        raw = {
            "source": row["source"],
            "source_id": row["source_id"],
            "url": row["canonical_url"],
            "title": row["title"],
            "body": row["text_excerpt"],
            "photos": [row["thumb_url"]] if row["thumb_url"] else [],
            "posted_at": row["posted_at"],
            "last_seen_at": row["last_seen_at"],
            "location": row["locality"],
            "distance_miles": row["distance_miles"],
            "tags": tags if isinstance(tags, list) else [],
        }
        record = normalize_record(raw)
        if record is None:
            continue
        record["listing_id"] = int(row["id"])
        record["review_status"] = row["review_status"]
        records.append(record)
    return records


def _recentness(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def generate_drafts(
    db_path: str | Path = DEFAULT_DB_PATH,
    profile: dict[str, Any] | None = None,
    limit: int = 3,
    now: datetime | None = None,
    dry: bool = False,
) -> list[dict[str, Any]]:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    profile = load_profile() if profile is None else profile
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timestamp = current.isoformat(timespec="seconds")
    self_stats = _self_stats(profile)
    migrate(db_path)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        records = _load_records(connection)
        records = dedupe_records(records)
        active = [record for record in records if not record.get("superseded_by")]
        score_rows = score_records(active, profile, current, db_path, cache=False, model_url="")
        scores = {row["id"]: row for row in score_rows}
        stats_required = {"doublelist", "grindr", "grindr-via-notification-bridge"}
        ranked = [
            record
            for record in active
            if not scores.get(record["id"], {}).get("hard_zero")
            and (record["source"] not in stats_required or self_stats)
        ]
        ranked.sort(
            key=lambda record: (
                -int(scores.get(record["id"], {}).get("score", 0)),
                -_recentness(record.get("last_seen_at")),
                str(record["id"]),
            )
        )
        selected = ranked[:limit]
        result: list[dict[str, Any]] = []
        for record in selected:
            variants = draft_variants(record, self_stats)
            listing_id = int(record["listing_id"])
            if not dry:
                exists = connection.execute(
                    "SELECT 1 FROM drafts WHERE listing_id = ? LIMIT 1",
                    (listing_id,),
                ).fetchone()
                if not exists:
                    for body in variants:
                        connection.execute(
                            "INSERT INTO drafts (listing_id, body, status, created_at, updated_at) VALUES (?, ?, 'draft', ?, ?)",
                            (listing_id, body, timestamp, timestamp),
                        )
            result.append(
                {
                    "listing_id": listing_id,
                    "source": record["source"],
                    "title": record["title"],
                    "score": int(scores.get(record["id"], {}).get("score", 0)),
                    "variants": list(variants),
                }
            )
        if not dry:
            connection.commit()
        return result
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _canonical_contact_url(value: str) -> str:
    parts = urlsplit(value.strip())
    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host or parts.username or parts.password:
        raise ValueError("A full HTTP(S) listing URL is required")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Listing URL has an invalid port") from None
    netloc = host if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/") or "/", parts.query, ""))


def approve_draft(
    draft_id: int,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    method: str = "clipboard",
) -> str:
    if method not in {"clipboard", "telegram-approval"}:
        raise ValueError("unsupported approval method")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timestamp = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    migrate(db_path)
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT drafts.listing_id, drafts.body, drafts.status, listings.review_status "
            "FROM drafts JOIN listings ON listings.id = drafts.listing_id WHERE drafts.id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise LookupError("draft not found")
        listing_id, body, draft_status, review_status = row
        if draft_status != "draft":
            raise ValueError("draft is no longer awaiting approval")
        if review_status in {"hidden", "contacted"}:
            raise ValueError("listing is hidden or already contacted")
        connection.execute(
            "UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ? AND status = 'draft'",
            (timestamp, draft_id),
        )
        connection.execute(
            "INSERT INTO contacts (listing_id, contacted_at, method, notes, created_at, status, ts) "
            "VALUES (?, ?, ?, '', ?, 'sent', ?)",
            (listing_id, timestamp, method, timestamp, timestamp),
        )
        connection.execute(
            "UPDATE listings SET review_status = 'contacted' WHERE id = ?",
            (listing_id,),
        )
        connection.commit()
        return str(body)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def edit_draft(
    draft_id: int,
    body: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> None:
    text = _clean_text(body, 4000)
    if not text or len(text) > 4000:
        raise ValueError("edited draft must contain 1–4000 characters")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timestamp = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    migrate(db_path)
    with sqlite3.connect(str(db_path), timeout=30) as connection:
        cursor = connection.execute(
            "UPDATE drafts SET body = ?, updated_at = ? WHERE id = ? AND status = 'draft'",
            (text, timestamp, draft_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("active draft not found")


def skip_draft(
    draft_id: int,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timestamp = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    migrate(db_path)
    with sqlite3.connect(str(db_path), timeout=30) as connection:
        cursor = connection.execute(
            "UPDATE drafts SET status = 'discarded', updated_at = ? WHERE id = ? AND status = 'draft'",
            (timestamp, draft_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("active draft not found")


def log_contact(
    url: str,
    status: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> int:
    if status not in {"replied", "ghosted", "met"}:
        raise ValueError("status must be replied, ghosted, or met")
    target = _canonical_contact_url(url)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timestamp = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    migrate(db_path)
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        matches = connection.execute(
            "SELECT id, canonical_url FROM listings ORDER BY last_seen_at DESC, id DESC"
        ).fetchall()
        listing_id = next(
            (int(row[0]) for row in matches if _canonical_contact_url(str(row[1])) == target),
            None,
        )
        if listing_id is None:
            raise LookupError("listing URL not found")
        contact = connection.execute(
            "SELECT id FROM contacts WHERE listing_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        if contact:
            connection.execute(
                "UPDATE contacts SET status = ?, ts = ? WHERE id = ?",
                (status, timestamp, contact[0]),
            )
        else:
            connection.execute(
                "INSERT INTO contacts (listing_id, contacted_at, method, notes, created_at, status, ts) "
                "VALUES (?, ?, 'manual-log', '', ?, ?, ?)",
                (listing_id, timestamp, timestamp, status, timestamp),
            )
        connection.execute(
            "UPDATE listings SET review_status = 'contacted' WHERE id = ?",
            (listing_id,),
        )
        connection.commit()
        return listing_id
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate two unsent, source-styled listing drafts")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        bundles = generate_drafts(args.db, profile, args.limit, dry=args.dry)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Draft generation failed: {error}", file=sys.stderr)
        return 1
    variant_count = 0
    for bundle in bundles:
        print(f"[{bundle['source']} | score {bundle['score']}] {bundle['title']}")
        for index, variant in enumerate(bundle["variants"], 1):
            print(f"Variant {index}: {variant}")
            variant_count += 1
        print()
    action = "would store" if args.dry else "stored"
    print(f"Listings: {len(bundles)}; drafts {action}: {variant_count}")
    if not self_stats_configured(profile):
        print("No explicit self-stats are configured; Doublelist and Grindr drafts were skipped.")
    return 0


def self_stats_configured(profile: dict[str, Any]) -> bool:
    return bool(_self_stats(profile))


if __name__ == "__main__":
    raise SystemExit(main())