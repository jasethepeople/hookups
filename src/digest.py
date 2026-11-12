from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, StrictUndefined

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.score import score_records

DEFAULT_THRESHOLD = 50
DEFAULT_LIMIT = 8
DEFAULT_SEED = PROJECT_ROOT / "tests" / "fixtures" / "digest_seed.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "out" / "digest.txt"
GENERATED_RE = re.compile(r"^Generated \(UTC\):\s*(.+?)\s*$", re.M)
TEMPLATE = """PERSONAL LISTING DIGEST{% if demo %} — DEMO SEED DATA{% endif %}
Generated (UTC): {{ generated_at }}
Threshold: {{ threshold }}/100 or higher; showing at most {{ limit }} listings.
New since last: {{ new_header }}
Suppressed count: {{ suppressed_count }}
{% for source, entries in groups %}
{{ source }} ({{ entries|length }})
{% for entry in entries %}
{{ entry.title }}
Score: {{ entry.score }} | Distance: {{ entry.distance }} | Age: {{ entry.age }}
Why: {{ entry.why }}
URL: {{ entry.url }}
ID: {{ entry.id }}
{% endfor %}{% endfor %}{% if not groups %}
No listings meet the threshold.
{% endif %}"""


def _clean(value: object, fallback: str = "Not listed") -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    return text or fallback


def _previous_generated_at(previous_digest: str | None) -> str | None:
    if not previous_digest:
        return None
    generated = GENERATED_RE.search(previous_digest)
    return generated.group(1).strip() if generated else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_url(value: object) -> str:
    url = _clean(value, "")
    if url.startswith(("https://", "http://")):
        return url
    return "Unavailable"


def prepare_entries(
    records: Iterable[dict[str, Any]],
    scores: Iterable[dict[str, Any]],
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], int]:
    if not 0 <= threshold <= 100:
        raise ValueError("threshold must be between 0 and 100")
    if limit < 0:
        raise ValueError("limit must not be negative")
    score_by_id: dict[str, dict[str, Any]] = {}
    suppressed_ids: set[str] = set()
    for result in scores:
        record_id = _clean(result.get("id"), "")
        if not record_id:
            continue
        score_by_id[record_id] = result
        if result.get("hard_zero"):
            suppressed_ids.add(record_id)
    eligible: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in records:
        record_id = _clean(record.get("id"), "")
        result = score_by_id.get(record_id)
        if not record_id or record_id in seen_ids or result is None:
            continue
        seen_ids.add(record_id)
        if record.get("superseded_by"):
            suppressed_ids.add(record_id)
            continue
        if result.get("hard_zero"):
            suppressed_ids.add(record_id)
            continue
        score = int(result.get("score", 0))
        if score < threshold:
            continue
        eligible.append(
            {
                "id": record_id,
                "source": _clean(record.get("source"), "unknown").lower(),
                "title": _clean(record.get("title")),
                "score": score,
                "distance": (
                    f"{float(record['distance_miles']):g} miles"
                    if isinstance(record.get("distance_miles"), (int, float))
                    else "Not listed"
                ),
                "age": _clean(record.get("age")),
                "why": _clean(result.get("why")),
                "url": _safe_url(record.get("url")),
                "posted_at": _clean(record.get("posted_at"), ""),
                "last_seen_at": _clean(record.get("last_seen_at"), ""),
            }
        )
    eligible.sort(key=lambda item: (-item["score"], item["source"], item["posted_at"], item["id"]))
    return eligible[:limit], len(suppressed_ids)


def render_digest(
    records: Iterable[dict[str, Any]],
    scores: Iterable[dict[str, Any]],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    suppressed_count: int = 0,
    previous_digest: str | None = None,
    generated_at: datetime | None = None,
    demo: bool = False,
) -> str:
    if suppressed_count < 0:
        raise ValueError("suppressed_count must not be negative")
    entries, score_suppressed = prepare_entries(records, scores, threshold, limit)
    previous_generated = _previous_generated_at(previous_digest)
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    cutoff = _parse_timestamp(previous_generated)
    new_count = sum(
        (seen_at := _parse_timestamp(entry.get("last_seen_at"))) is not None
        and (cutoff is None or seen_at > cutoff)
        for entry in entries
    )
    new_header = (
        f"{new_count} listing(s)"
        if previous_generated
        else f"first digest; {new_count} included listing(s) with last-seen times are new"
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["source"], []).append(entry)
    groups = sorted(grouped.items())
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    template = environment.from_string(TEMPLATE)
    return template.render(
        demo=demo,
        generated_at=now.isoformat(timespec="seconds"),
        threshold=threshold,
        limit=limit,
        new_header=new_header,
        last_digest=previous_generated,
        suppressed_count=suppressed_count + score_suppressed,
        groups=groups,
    ).strip() + "\n"


def render_seed_file(
    seed_path: str | Path = DEFAULT_SEED,
    output_path: str | Path = DEFAULT_OUTPUT,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> str:
    seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    records = seed.get("records")
    profile = seed.get("profile", {})
    if not isinstance(records, list) or not isinstance(profile, dict):
        raise ValueError("seed file must contain a records list and profile mapping")
    scores = score_records(records, profile, now=now, cache=False, model_url="")
    output = Path(output_path)
    previous_digest = output.read_text(encoding="utf-8") if output.exists() else None
    text = render_digest(
        records,
        scores,
        threshold=threshold,
        limit=limit,
        suppressed_count=int(seed.get("suppressed_count", 0)),
        previous_digest=previous_digest,
        generated_at=now,
        demo=bool(seed.get("demo", False)),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    text = render_seed_file(args.seed, args.output, args.threshold, args.limit)
    sys.stdout.write(text)
    print(f"Rendered {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
