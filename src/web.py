from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, select_autoescape

from src.db import DEFAULT_DB_PATH, migrate
from src.draft import approve_draft as approve_draft_record, edit_draft as edit_draft_record, generate_drafts, skip_draft as skip_draft_record
from src.dedupe import dedupe_records
from src.normalize import normalize_record
from src.score import load_profile, model_url_from_env, score_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("HOOKUPS_DB_PATH", str(DEFAULT_DB_PATH)))
DEMO_MODE = os.environ.get("HOOKUPS_DEMO", "0").strip() == "1"
CSRF_TOKENS: dict[str, datetime] = {}
CSRF_LOCK = threading.Lock()
TEMPLATE = Environment(autoescape=select_autoescape(["html", "xml"]))
DEMO_PROFILE = {
    "max_drive_minutes": 60,
    "age_range": {"min_age": 24, "max_age": 45},
    "body_types": ["athletic", "average", "fit", "curvy"],
    "positions": ["versatile", "top", "bottom"],
    "must_haves": [],
    "dealbreakers": [],
    "host_or_travel": "either",
    "discretion_level": "discreet",
}
SOURCES = ("locanto", "doublelist", "squirt", "reddit")


PAGE = TEMPLATE.from_string("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="action-token" content="{{ action_token }}">
<title>Hookups — private feed</title>
<style>
:root{color-scheme:dark;--bg:#0b1016;--panel:#121b25;--panel2:#172330;--line:#263744;--ink:#eef5f4;--muted:#91a4ad;--accent:#a8efc2;--accent2:#6fd4bd;--warning:#ffc875;--danger:#ff958c}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 16% -10%,#1b3540 0,transparent 43%),var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:1380px;margin:0 auto;padding:36px 28px 64px}.topline{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:38px}.brand{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);font-weight:750}.brand b{color:var(--accent);font-size:15px;letter-spacing:.12em}.demo{border:1px solid #78633c;background:#241e15;color:#ffd492;padding:6px 11px;border-radius:999px;font-size:12px;letter-spacing:.04em}.hero{display:flex;justify-content:space-between;align-items:end;gap:24px;margin-bottom:26px}.eyebrow{color:var(--accent2);font-weight:700;letter-spacing:.15em;text-transform:uppercase;font-size:11px}.hero h1{font-size:clamp(32px,5vw,54px);line-height:1.02;letter-spacing:-.045em;margin:8px 0 10px}.hero p{margin:0;color:var(--muted);max-width:620px}.count{min-width:145px;text-align:right;color:var(--muted);font-size:13px}.count strong{display:block;color:var(--ink);font-size:28px;line-height:1.1}.filters{display:flex;align-items:end;gap:12px;flex-wrap:wrap;padding:16px;background:rgba(18,27,37,.94);border:1px solid var(--line);border-radius:16px;margin:26px 0}.filters label{display:grid;gap:5px;color:var(--muted);font-size:12px}.filters select,.filters input{height:40px;min-width:160px;padding:0 11px;border:1px solid #344856;border-radius:9px;background:#0d151c;color:var(--ink);font:inherit}.filters input{min-width:110px}.button,.filters button{height:40px;border:1px solid #446258;border-radius:9px;padding:0 14px;color:#06110b;background:var(--accent);font-weight:750;cursor:pointer}.filters .reset{height:40px;display:inline-flex;align-items:center;padding:0 12px;color:var(--muted);border:1px solid var(--line);border-radius:9px}.results{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{min-width:0;background:linear-gradient(155deg,rgba(23,35,48,.97),rgba(17,25,34,.98));border:1px solid var(--line);border-radius:17px;padding:19px 20px 17px;box-shadow:0 12px 36px #0002}.card-head{display:flex;justify-content:space-between;gap:14px;align-items:start}.source{display:inline-flex;align-items:center;gap:7px;color:var(--accent2);font-size:11px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}.source:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent2);box-shadow:0 0 12px #6fd4bd80}.card h2{font-size:19px;line-height:1.25;letter-spacing:-.02em;margin:7px 0 5px}.score{flex:none;text-align:right;color:var(--accent);font-size:26px;font-weight:800;line-height:1}.score small{display:block;color:var(--muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase;margin-top:4px}.meta{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:12px 0}.pill{padding:4px 8px;background:#0c151c;border:1px solid #273946;border-radius:999px}.body{color:#c5d2d5;white-space:pre-wrap;overflow-wrap:anywhere;margin:13px 0 14px;font-size:14px}.why{padding:11px 12px;border-left:2px solid var(--accent2);background:#0c151c;color:#aebdc2;border-radius:0 9px 9px 0;font-size:12px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}.actions button{height:34px;padding:0 11px;border-radius:8px;background:#17242c;color:var(--ink);border:1px solid #344751;font:inherit;font-size:12px;font-weight:650;cursor:pointer}.actions button:hover:not(:disabled){border-color:var(--accent2);color:var(--accent)}.actions button:disabled{color:#69777c;cursor:not-allowed;opacity:.7}.status{color:var(--warning);font-size:11px;margin-left:auto;align-self:center}.empty{grid-column:1/-1;padding:46px 24px;text-align:center;border:1px dashed #344956;border-radius:16px;color:var(--muted)}.footer{margin-top:34px;color:#71848b;font-size:11px;text-align:center}.error{color:var(--danger)}@media(max-width:840px){.results{grid-template-columns:1fr}.hero{align-items:start;flex-direction:column}.count{text-align:left}.wrap{padding:24px 16px 48px}}
</style><style>
.drafts{margin:16px 0;padding:12px;background:#0c151c;border:1px solid #344751;border-radius:12px}.drafts summary{cursor:pointer;color:var(--accent);font-weight:700}.draft-item{padding-top:12px}.draft-item label{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}.draft-item textarea{width:100%;min-height:82px;padding:10px;border:1px solid #344856;border-radius:9px;background:#0a1218;color:var(--ink);font:inherit;resize:vertical}.draft-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}.draft-actions button{height:32px;padding:0 10px;border-radius:8px;background:#17242c;color:var(--ink);border:1px solid #344751;font:inherit;font-size:12px;font-weight:650;cursor:pointer}.draft-actions button:first-child{background:var(--accent);color:#06110b;border-color:var(--accent)}.draft-actions button:disabled{opacity:.6;cursor:wait}
</style>
</head>
<body>
<div class="wrap">
<header class="topline"><div class="brand"><b>H /</b> private discovery feed</div>{% if demo_mode %}<div class="demo">DEMO DATA · 10 SYNTHETIC LISTINGS</div>{% else %}<div class="demo">PRIVATE · LOCAL FEED</div>{% endif %}</header>
<section class="hero"><div><div class="eyebrow">Review at your pace</div><h1>Your feed, without the noise.</h1><p>Ranked listings with transparent score explanations. Nothing here sends a message; review actions stay in this private dashboard.</p></div><div class="count"><strong>{{ listings|length }}</strong> listings shown</div></section>
<form class="filters" method="get" action="/">
<label>Source<select name="source"><option value="">All sources</option>{% for item in sources %}<option value="{{ item }}" {% if selected_source == item %}selected{% endif %}>{{ item|title }}</option>{% endfor %}</select></label>
<label>Minimum score<input type="number" name="min_score" min="0" max="100" step="1" value="{{ min_score }}"></label>
<button type="submit">Apply filters</button><a class="reset" href="/">Reset</a>
</form>
<main class="results">
{% for item in listings %}
<article class="card" id="listing-{{ item.listing_id }}">
<div class="card-head"><div><div class="source">{{ item.source }}</div><h2>{{ item.title }}</h2></div><div class="score">{{ item.score }}<small>score / 100</small></div></div>
<div class="meta"><span class="pill">{% if item.age is not none %}Age {{ item.age }}{% else %}Age not listed{% endif %}</span><span class="pill">{% if item.distance_miles is not none %}{{ "%.1f"|format(item.distance_miles) }} mi{% else %}Distance not listed{% endif %}</span><span class="pill">{{ item.location or "Location not listed" }}</span><span class="pill">Seen {{ item.last_seen }}</span><span class="pill">{{ item.photo_count }} photo link{% if item.photo_count != 1 %}s{% endif %}</span></div>
<p class="body">{{ item.body or "No description provided." }}</p><div class="why">{{ item.why }}</div>
{% if item.drafts %}<details class="drafts"><summary>Unsent drafts ({{ item.drafts|length }})</summary>{% for draft in item.drafts %}<div class="draft-item"><label for="draft-{{ draft.id }}">Variant {{ loop.index }}</label><textarea id="draft-{{ draft.id }}">{{ draft.body }}</textarea><div class="draft-actions"><button type="button" data-action="approve-draft" data-id="{{ draft.id }}">Approve · copy</button><button type="button" data-action="edit-draft" data-id="{{ draft.id }}">Save edit</button><button type="button" data-action="skip-draft" data-id="{{ draft.id }}">Skip</button><span class="status" data-draft-status="{{ draft.id }}"></span></div></div>{% endfor %}</details>{% endif %}
<div class="actions"><a class="button" href="{{ item.url }}" target="_blank" rel="noopener noreferrer">Open source ↗</a><button type="button" data-action="seen" data-id="{{ item.listing_id }}">Seen</button><button type="button" data-action="hide" data-id="{{ item.listing_id }}">Hide</button><button type="button" data-action="block-domain" data-id="{{ item.listing_id }}">Block domain</button><button type="button" data-action="copy-opener" data-opener="{{ opener_for_loop[loop.index0] }}" {% if not opener_for_loop[loop.index0] %}disabled title="Add one of your own recent openers to the profile to enable"{% endif %}>Copy opener</button><span class="status">{{ item.review_status }}</span></div>
</article>
{% else %}<div class="empty">No listings match these filters.</div>{% endfor %}
</main>
<footer class="footer">Private by default · No outbound messages · Source text is shown without remote images.</footer>
</div>
<script>
const actionToken = document.querySelector('meta[name="action-token"]').content;
document.querySelectorAll('button[data-action]').forEach(button => button.addEventListener('click', async () => {
  if (button.disabled) return;
  const action = button.dataset.action;
  if (action === 'copy-opener') {
    try { await navigator.clipboard.writeText(button.dataset.opener); button.textContent = 'Copied'; }
    catch { button.textContent = 'Clipboard unavailable'; }
    return;
  }
  const id = button.dataset.id;
  const status = document.querySelector(`[data-draft-status="${id}"]`);
  const textarea = document.getElementById(`draft-${id}`);
  if (action === 'approve-draft') {
    try { await navigator.clipboard.writeText(textarea.value); }
    catch { if (status) status.textContent = 'Clipboard unavailable; not approved'; return; }
  }
  button.disabled = true;
  let path = `/api/listings/${id}/${action}`;
  const headers = {'X-Action-Token': actionToken, 'Accept': 'application/json'};
  const options = {method: 'POST', headers};
  if (action === 'approve-draft' || action === 'skip-draft') path = `/api/drafts/${id}/${action.replace('-draft', '')}`;
  if (action === 'edit-draft') {
    path = `/api/drafts/${id}/edit`;
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify({body: textarea.value});
  }
  try {
    const response = await fetch(path, options);
    if (response.ok) location.reload();
    else { button.disabled = false; if (status) status.textContent = 'Could not save; reload and retry'; }
  } catch {
    button.disabled = false;
    if (status) status.textContent = 'Connection failed';
  }
}));
</script>
</body>
</html>""")


DEMO_ROWS = [
    {"source": "locanto", "slug": "sample-01", "title": "Easygoing coffee and conversation", "age": 34, "distance": 2.1, "location": "Demo District North", "body": "Age 34, athletic and versatile. I can host and prefer a discreet, low-key first meet. Here for a kind conversation and a relaxed coffee.", "tags": ["body_type:athletic", "position:versatile"], "photo": True, "hours": 1},
    {"source": "doublelist", "slug": "sample-02", "title": "Friendly, active, and open to meeting", "age": 29, "distance": 6.4, "location": "Demo District East", "body": "Age 29, fit, top. Can travel and enjoy an easygoing first hello. Discreet and respectful; happy to keep things simple.", "tags": ["body_type:fit", "position:top", "travel"], "photo": True, "hours": 2},
    {"source": "reddit", "slug": "sample-03", "title": "Looking for a warm, low-key connection", "age": 38, "distance": 4.8, "location": "Demo District Central", "body": "Age 38, average build, versatile. My place is available for a private, discreet meet after we chat.", "tags": ["body_type:average", "position:versatile", "host"], "photo": False, "hours": 3},
    {"source": "squirt", "slug": "sample-04", "title": "Active and considerate, seeking company", "age": 31, "distance": 9.3, "location": "Demo District South", "body": "Age 31, athletic, bottom. Can host at my place; looking for a respectful and private connection.", "tags": ["body_type:athletic", "position:bottom", "host"], "photo": True, "hours": 4},
    {"source": "locanto", "slug": "sample-05", "title": "A relaxed meetup with good conversation", "age": 42, "distance": 12.0, "location": "Demo District West", "body": "Age 42, curvy and versatile. Can travel to you. Prefer low-key plans and clear communication.", "tags": ["body_type:curvy", "position:versatile", "travel"], "photo": True, "hours": 5},
    {"source": "doublelist", "slug": "sample-06", "title": "Thoughtful company, no rush", "age": 27, "distance": 16.0, "location": "Demo District North", "body": "Age 27, fit and bottom. I can host; private, discreet, and happy to talk first.", "tags": ["body_type:fit", "position:bottom", "host"], "photo": False, "hours": 6},
    {"source": "reddit", "slug": "sample-07", "title": "Seeking a kind, active person", "age": 36, "distance": 7.7, "location": "Demo District East", "body": "Age 36, athletic and top. Can travel, enjoy a low-key meet, and value discretion.", "tags": ["body_type:athletic", "position:top", "travel"], "photo": True, "hours": 7},
    {"source": "squirt", "slug": "sample-08", "title": "Easygoing and discreet", "age": 44, "distance": 21.5, "location": "Demo District Central", "body": "Age 44, average build, versatile. Can host and prefer a private first meeting with a little conversation.", "tags": ["body_type:average", "position:versatile", "host"], "photo": True, "hours": 8},
    {"source": "locanto", "slug": "sample-09", "title": "Friendly and spontaneous", "age": 25, "distance": 28.2, "location": "Demo District South", "body": "Age 25, athletic and bottom. Can travel; looking for a discreet, respectful meetup.", "tags": ["body_type:athletic", "position:bottom", "travel"], "photo": False, "hours": 9},
    {"source": "doublelist", "slug": "sample-10", "title": "Calm company for a casual evening", "age": 40, "distance": 11.6, "location": "Demo District West", "body": "Age 40, curvy and versatile. My place is available; private and easygoing, happy to talk first.", "tags": ["body_type:curvy", "position:versatile", "host"], "photo": True, "hours": 10},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate(DB_PATH)
    if DEMO_MODE:
        seed_demo_data(DB_PATH)
    yield


app = FastAPI(title="Hookups Private Feed", docs_url=None, redoc_url=None, lifespan=lifespan)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _profile_for_scoring() -> dict:
    return DEMO_PROFILE if DEMO_MODE else load_profile()


def _canonical_domain(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _domain_blocked(host: str | None, blocked: set[str]) -> bool:
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in blocked)


def seed_demo_data(db_path: str | Path = DB_PATH, now: datetime | None = None) -> int:
    migrate(db_path)
    current = now or _now()
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute("SELECT COUNT(*) FROM listings WHERE source_id LIKE 'demo-%'").fetchone()[0]
        if existing == len(DEMO_ROWS):
            for index, item in enumerate(DEMO_ROWS, start=1):
                thumb_url = f"https://{item['source']}.demo.invalid/photo-{index:02d}.jpg" if item["photo"] else None
                connection.execute("UPDATE listings SET thumb_url = ? WHERE source_id = ?", (thumb_url, f"demo-{index:02d}"))
            connection.commit()
            generate_drafts(db_path, profile=DEMO_PROFILE, limit=3, now=current)
            return existing
        if existing:
            ids = [row[0] for row in connection.execute("SELECT id FROM listings WHERE source_id LIKE 'demo-%'")]
            connection.executemany("DELETE FROM listings WHERE id = ?", [(row_id,) for row_id in ids])
        normalized: list[dict] = []
        records_to_insert: list[tuple] = []
        for index, item in enumerate(DEMO_ROWS, start=1):
            url = f"https://{item['source']}.demo.invalid/{item['slug']}"
            posted = current - timedelta(hours=item["hours"] + 1)
            last_seen = current - timedelta(hours=item["hours"])
            raw = {
                "source": item["source"],
                "source_id": f"demo-{index:02d}",
                "url": url,
                "title": item["title"],
                "body": item["body"],
                "age": item["age"],
                "location": item["location"],
                "distance_miles": item["distance"],
                "tags": item["tags"],
                "photos": [f"https://{item['source']}.demo.invalid/photo-{index:02d}.jpg"] if item["photo"] else [],
                "posted_at": posted.isoformat(timespec="seconds"),
                "last_seen_at": last_seen.isoformat(timespec="seconds"),
            }
            record = normalize_record(raw, current)
            if record is None:
                raise RuntimeError(f"Demo listing {index} was unexpectedly suppressed")
            normalized.append(record)
            dedupe_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
            thumb_url = raw["photos"][0] if raw["photos"] else None
            records_to_insert.append((item["source"], dedupe_key, raw["source_id"], url, item["title"], item["body"], thumb_url, posted.isoformat(timespec="seconds"), current.isoformat(timespec="seconds"), last_seen.isoformat(timespec="seconds"), item["location"], item["distance"], json.dumps(item["tags"]), "new"))
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO listings (source, dedupe_key, source_id, canonical_url, title, text_excerpt, thumb_url, posted_at, first_seen_at, last_seen_at, locality, distance_miles, tags_json, review_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            records_to_insert,
        )
        connection.commit()
        score_records(normalized, DEMO_PROFILE, current, db_path, cache=True, model_url="")
        generate_drafts(db_path, profile=DEMO_PROFILE, limit=3, now=current)
        return len(normalized)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        if Path(db_path).is_file():
            os.chmod(db_path, 0o600)


def _read_records(db_path: str | Path, profile: dict, now: datetime) -> list[dict]:
    migrate(db_path)
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        blocked = {row[0] for row in connection.execute("SELECT domain FROM blocked_domains")}
        rows = connection.execute("SELECT * FROM listings WHERE review_status NOT IN ('hidden', 'contacted') ORDER BY last_seen_at DESC").fetchall()
    finally:
        connection.close()
    records: list[dict] = []
    for row in rows:
        if _domain_blocked(_canonical_domain(row["canonical_url"]), blocked):
            continue
        raw = {
            "source": row["source"],
            "source_id": row["source_id"],
            "url": row["canonical_url"],
            "title": row["title"],
            "body": row["text_excerpt"],
            "location": row["locality"],
            "distance_miles": row["distance_miles"],
            "tags": _json_list(row["tags_json"]),
            "photos": [row["thumb_url"]] if row["thumb_url"] else [],
            "posted_at": row["posted_at"],
            "last_seen_at": row["last_seen_at"],
        }
        record = normalize_record(raw, now)
        if record is None:
            continue
        record["listing_id"] = row["id"]
        record["review_status"] = row["review_status"]
        records.append(record)
    return dedupe_records(records)


def _timestamp_sort(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def feed_records(db_path: str | Path = DB_PATH, source: str | None = None, min_score: int = 0, now: datetime | None = None) -> list[dict]:
    current = now or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    profile = _profile_for_scoring()
    records = _read_records(db_path, profile, current)
    model_url = "" if DEMO_MODE else model_url_from_env()
    scores = score_records(records, profile, current, db_path, cache=True, model_url=model_url)
    by_id = {result["id"]: result for result in scores}
    output: list[dict] = []
    for record in records:
        score = by_id.get(record["id"])
        if not score or score["hard_zero"] or record.get("superseded_by"):
            continue
        if score["score"] < min_score or (source and record["source"] != source):
            continue
        row = {**record, **score}
        row["last_seen"] = record.get("last_seen_at", "")[:16].replace("T", " ")
        row["photo_count"] = len(record.get("photos") or [])
        output.append(row)
    output.sort(key=lambda item: (-item["score"], -_timestamp_sort(item.get("last_seen_at"))))
    return output


def drafts_for_listings(db_path: str | Path, listings: list[dict]) -> dict[int, list[dict]]:
    listing_ids = [int(item["listing_id"]) for item in listings]
    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    migrate(db_path)
    with sqlite3.connect(str(db_path), timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT id, listing_id, body FROM drafts WHERE status = 'draft' AND listing_id IN ({placeholders}) ORDER BY listing_id, id",
            listing_ids,
        ).fetchall()
    result: dict[int, list[dict]] = {}
    for row in rows:
        result.setdefault(int(row["listing_id"]), []).append(dict(row))
    return result



def _new_action_token() -> str:
    token = secrets.token_urlsafe(32)
    cutoff = _now() + timedelta(hours=1)
    with CSRF_LOCK:
        for old, expires in list(CSRF_TOKENS.items()):
            if expires <= _now():
                del CSRF_TOKENS[old]
        CSRF_TOKENS[token] = cutoff
    return token


def _require_action_token(token: str | None) -> None:
    current = _now()
    with CSRF_LOCK:
        expires = CSRF_TOKENS.pop(token or "", None)
    if expires is None or expires <= current:
        raise HTTPException(status_code=403, detail="Action token expired; reload the page")


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    source: str = Query(""),
    min_score: int = Query(0, ge=0, le=100),
) -> HTMLResponse:
    sources = sorted({item["source"] for item in feed_records(DB_PATH, min_score=0)})
    selected = source if source in sources else ""
    listings = feed_records(DB_PATH, source=selected or None, min_score=min_score)
    drafts = drafts_for_listings(DB_PATH, listings)
    for item in listings:
        item["drafts"] = drafts.get(int(item["listing_id"]), [])
    openers = [] if DEMO_MODE else load_profile().get("recent_openers", [])
    if not isinstance(openers, list):
        openers = []
    openers = [str(item).strip() for item in openers if isinstance(item, str) and item.strip()]
    opener_for_loop = [openers[index % len(openers)] if openers else "" for index in range(len(listings))]
    html = PAGE.render(
        listings=listings,
        sources=sources,
        selected_source=selected,
        min_score=min_score,
        demo_mode=DEMO_MODE,
        opener_for_loop=opener_for_loop,
        action_token=_new_action_token(),
    )
    return HTMLResponse(html)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "demo": DEMO_MODE})


def _update_review_status(listing_id: int, status: str) -> None:
    if status not in {"seen", "hidden"}:
        raise ValueError("unsupported review status")
    migrate(DB_PATH)
    with sqlite3.connect(str(DB_PATH), timeout=30) as connection:
        cursor = connection.execute("UPDATE listings SET review_status = ? WHERE id = ?", (status, listing_id))
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Listing not found")


@app.post("/api/listings/{listing_id}/seen")
def mark_seen(listing_id: int, x_action_token: str | None = Header(None)) -> JSONResponse:
    _require_action_token(x_action_token)
    _update_review_status(listing_id, "seen")
    return JSONResponse({"ok": True, "status": "seen"})


@app.post("/api/listings/{listing_id}/hide")
def hide_listing(listing_id: int, x_action_token: str | None = Header(None)) -> JSONResponse:
    _require_action_token(x_action_token)
    _update_review_status(listing_id, "hidden")
    return JSONResponse({"ok": True, "status": "hidden"})


@app.post("/api/listings/{listing_id}/block-domain")
def block_domain(listing_id: int, x_action_token: str | None = Header(None)) -> JSONResponse:
    _require_action_token(x_action_token)
    migrate(DB_PATH)
    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        row = connection.execute("SELECT canonical_url FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        domain = _canonical_domain(row[0])
        if not domain or not re.fullmatch(r"[a-z0-9.-]{1,253}", domain):
            raise HTTPException(status_code=400, detail="Listing has no blockable domain")
        connection.execute("INSERT OR IGNORE INTO blocked_domains (domain, blocked_at) VALUES (?, ?)", (domain, _now().isoformat(timespec="seconds")))
        rows = connection.execute("SELECT id, canonical_url FROM listings").fetchall()
        to_hide = [item[0] for item in rows if _domain_blocked(_canonical_domain(item[1]), {domain})]
        connection.executemany("UPDATE listings SET review_status = 'hidden' WHERE id = ? AND review_status != 'contacted'", [(item,) for item in to_hide])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return JSONResponse({"ok": True, "blocked_domain": domain, "hidden": len(to_hide)})


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft_route(draft_id: int, x_action_token: str | None = Header(None)) -> JSONResponse:
    _require_action_token(x_action_token)
    try:
        approve_draft_record(draft_id, DB_PATH, method="clipboard")
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return JSONResponse({"ok": True, "status": "approved"})


@app.post("/api/drafts/{draft_id}/edit")
async def edit_draft_route(
    draft_id: int,
    request: Request,
    x_action_token: str | None = Header(None),
) -> JSONResponse:
    _require_action_token(x_action_token)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected a JSON body with a draft body") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str):
        raise HTTPException(status_code=400, detail="Expected a JSON body with a draft body")
    try:
        edit_draft_record(draft_id, payload["body"], DB_PATH)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return JSONResponse({"ok": True, "status": "edited"})


@app.post("/api/drafts/{draft_id}/skip")
def skip_draft_route(draft_id: int, x_action_token: str | None = Header(None)) -> JSONResponse:
    _require_action_token(x_action_token)
    try:
        skip_draft_record(draft_id, DB_PATH)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    return JSONResponse({"ok": True, "status": "skipped"})


def seed_demo_data_cli() -> int:
    return seed_demo_data(DB_PATH)
