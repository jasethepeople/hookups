from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import DEFAULT_DB_PATH, migrate
from src.dedupe import dedupe_records
from src.digest import DEFAULT_LIMIT, DEFAULT_THRESHOLD, DEFAULT_OUTPUT, render_digest
from src.normalize import normalize_record
from src.score import load_profile, score_records

ENV_PATH = PROJECT_ROOT / ".env"
OUTPUT_PATH = DEFAULT_OUTPUT
OFFSET_PATH = PROJECT_ROOT / "cache" / "telegram-offset.json"
LOCK_PATH = PROJECT_ROOT / "cache" / "telegram-notify.lock"
CHUNK_LIMIT = 3800
POLL_TIMEOUT = 20
COMMAND_PATTERN = re.compile(r"^/hookups(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)


class TelegramAPIError(RuntimeError):
    def __init__(self, retry_after: int | None = None):
        super().__init__("Telegram API request failed")
        self.retry_after = retry_after


def read_settings(env_path: str | Path = ENV_PATH) -> dict[str, str]:
    values = dotenv_values(env_path)
    return {
        "token": str(values.get("TELEGRAM_TOKEN") or "").strip(),
        "chat_id": str(values.get("TELEGRAM_CHAT_ID") or "").strip(),
        "model_url": str(values.get("MODEL_URL") or "").strip(),
    }


def _private_chat_id(value: str) -> bool:
    return value.isdecimal() and int(value) > 0


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _prefix_within_limit(value: str, limit: int) -> int:
    units = 0
    for index, character in enumerate(value):
        size = 2 if ord(character) > 0xFFFF else 1
        if units + size > limit:
            return index
        units += size
    return len(value)


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    if limit < 2:
        raise ValueError("limit must be at least two UTF-16 units")
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while _utf16_units(remaining) > limit:
        end = _prefix_within_limit(remaining, limit)
        if end == 0:
            raise ValueError("text contains a character that exceeds the chunk limit")
        boundary = max(remaining.rfind("\n", 0, end), remaining.rfind(" ", 0, end))
        if boundary >= end // 2:
            end = boundary + 1
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _decode_tags(value: object) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _listing_records(db_path: str | Path, now: datetime) -> tuple[list[dict[str, Any]], int]:
    migrate(db_path)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    records: list[dict[str, Any]] = []
    suppressed = 0
    try:
        rows = connection.execute(
            "SELECT source, source_id, canonical_url, title, text_excerpt, thumb_url, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status FROM listings ORDER BY id"
        ).fetchall()
        for row in rows:
            if row["review_status"] == "hidden":
                suppressed += 1
                continue
            raw = {
                "source": row["source"],
                "source_id": row["source_id"],
                "url": row["canonical_url"],
                "title": row["title"],
                "body": row["text_excerpt"],
                "text_excerpt": row["text_excerpt"],
                "photos": [row["thumb_url"]] if row["thumb_url"] else [],
                "location": row["locality"],
                "distance_miles": row["distance_miles"],
                "tags": _decode_tags(row["tags_json"]),
                "posted_at": row["posted_at"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
            try:
                record = normalize_record(raw, now=now)
            except (TypeError, ValueError):
                record = None
            if record is None:
                suppressed += 1
            else:
                records.append(record)
    finally:
        connection.close()
    return dedupe_records(records), suppressed


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def render_live_digest(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = OUTPUT_PATH,
    model_url: str = "",
    now: datetime | None = None,
    write: bool = True,
) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    records, normalization_suppressed = _listing_records(db_path, current)
    profile = load_profile()
    scores = score_records(
        records,
        profile,
        now=current,
        db_path=db_path,
        cache=True,
        model_url=model_url,
    )
    output = Path(output_path)
    previous = output.read_text(encoding="utf-8") if output.is_file() else None
    if previous and previous.startswith("PERSONAL LISTING DIGEST — DEMO SEED DATA"):
        previous = None
    text = render_digest(
        records,
        scores,
        threshold=DEFAULT_THRESHOLD,
        limit=DEFAULT_LIMIT,
        suppressed_count=normalization_suppressed,
        previous_digest=previous,
        generated_at=current,
    )
    if write:
        _write_private(output, text)
    return text


def _telegram_call(token: str, method: str, payload: dict[str, Any]) -> Any:
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = httpx.post(
            endpoint,
            json=payload,
            timeout=httpx.Timeout(35, connect=10),
            trust_env=False,
        )
    except httpx.HTTPError as error:
        raise TelegramAPIError() from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    retry_after = int(retry_after) if isinstance(retry_after, (int, float)) else None
    if response.status_code == 429 or response.status_code >= 500:
        raise TelegramAPIError(retry_after)
    if not response.is_success or not isinstance(data, dict) or not data.get("ok"):
        raise TelegramAPIError(retry_after)
    return data.get("result")


def _telegram_call_with_retry(token: str, method: str, payload: dict[str, Any]) -> Any:
    for attempt in range(3):
        try:
            return _telegram_call(token, method, payload)
        except TelegramAPIError as error:
            if attempt == 2:
                raise
            delay = error.retry_after if error.retry_after is not None else 2**attempt
            time.sleep(min(max(1, delay), 60))
    raise TelegramAPIError()


def send_digest(token: str, chat_id: str, text: str) -> int:
    chunks = chunk_text(text)
    for chunk in chunks:
        _telegram_call_with_retry(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
        )
    return len(chunks)


def is_hookups_command(update: dict[str, Any], chat_id: str) -> bool:
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return False
    if chat.get("type") != "private" or str(chat.get("id", "")) != chat_id:
        return False
    sender = message.get("from")
    if isinstance(sender, dict) and sender.get("is_bot") is True:
        return False
    text = message.get("text")
    return isinstance(text, str) and COMMAND_PATTERN.match(text.strip()) is not None


def handle_update(
    update: dict[str, Any],
    token: str,
    chat_id: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = OUTPUT_PATH,
    model_url: str = "",
    renderer: Callable[..., str] = render_live_digest,
    sender: Callable[[str, str, str], int] = send_digest,
) -> bool:
    if not is_hookups_command(update, chat_id):
        return False
    text = renderer(db_path=db_path, output_path=output_path, model_url=model_url, write=True)
    sender(token, chat_id, text)
    return True


def _load_offset(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0
    offset = value.get("offset") if isinstance(value, dict) else None
    return offset if isinstance(offset, int) and offset >= 0 else 0


def _save_offset(path: Path, offset: int) -> None:
    _write_private(path, json.dumps({"offset": offset}, separators=(",", ":")))


def _process_updates(
    updates: list[dict[str, Any]],
    token: str,
    chat_id: str,
    offset_path: Path,
    db_path: str | Path,
    output_path: str | Path,
    model_url: str,
) -> int:
    offset = _load_offset(offset_path)
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int) and update_id >= offset:
            offset = update_id + 1
            _save_offset(offset_path, offset)
        try:
            handle_update(update, token, chat_id, db_path, output_path, model_url)
        except (OSError, sqlite3.Error, ValueError, TelegramAPIError) as error:
            print(f"/hookups request failed ({type(error).__name__}); send the command again to retry.")
    return offset


def poll_once(
    token: str,
    chat_id: str,
    offset_path: str | Path = OFFSET_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = OUTPUT_PATH,
    model_url: str = "",
    timeout: int = POLL_TIMEOUT,
) -> int:
    offset_file = Path(offset_path)
    updates = _telegram_call_with_retry(
        token,
        "getUpdates",
        {"offset": _load_offset(offset_file), "timeout": timeout, "allowed_updates": ["message"]},
    )
    if not isinstance(updates, list):
        raise TelegramAPIError()
    return _process_updates(
        [update for update in updates if isinstance(update, dict)],
        token,
        chat_id,
        offset_file,
        db_path,
        output_path,
        model_url,
    )


def _acquire_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise RuntimeError("another notifier process is already running") from None
    return descriptor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    settings = read_settings(args.env)
    token = settings["token"]
    chat_id = settings["chat_id"]
    configured = bool(token) and _private_chat_id(chat_id)
    if args.dry or not configured:
        text = render_live_digest(
            args.database,
            args.output,
            model_url="",
            write=False,
        )
        sys.stdout.write(text)
        if configured:
            print("DRY MODE: no Telegram requests sent.")
        else:
            print("DRY MODE: set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to enable private-chat polling; no network requests made.")
        return 0
    lock_descriptor = -1
    try:
        if not args.once:
            lock_descriptor = _acquire_lock(LOCK_PATH)
        while True:
            poll_once(token, chat_id, OFFSET_PATH, args.database, args.output, settings["model_url"])
            if args.once:
                return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError, TelegramAPIError) as error:
        print(f"Notifier stopped ({type(error).__name__}).")
        return 1
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
