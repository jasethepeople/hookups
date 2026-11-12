from datetime import datetime, timedelta, timezone
import json

from src.digest import render_digest, render_seed_file
from src.score import score_records


SEED_PATH = "tests/fixtures/digest_seed.json"
NOW = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)


def seed_data():
    return json.loads(open(SEED_PATH, encoding="utf-8").read())


def test_seed_digest_renders_required_fields_and_suppressed_count(tmp_path):
    output_path = tmp_path / "digest.txt"
    text = render_seed_file(SEED_PATH, output_path, now=NOW)
    assert output_path.read_text(encoding="utf-8") == text
    assert "PERSONAL LISTING DIGEST — DEMO SEED DATA" in text
    assert "Threshold: 50/100 or higher; showing at most 8 listings." in text
    assert "New since last: first digest; 1 included listing(s) with last-seen times are new" in text
    assert "Suppressed count: 1" in text
    assert "reddit (1)" in text
    assert "Score: 100 | Distance: 0 miles | Age: 34" in text
    assert "Why: " in text
    assert "URL: https://example.invalid/1" in text
    assert "seed-2" not in text


def test_previous_digest_uses_generated_timestamp_cutoff():
    seed = seed_data()
    scores = score_records(seed["records"], seed["profile"], NOW, cache=False, model_url="")
    first = render_digest(seed["records"], scores, generated_at=NOW, demo=True)
    second = render_digest(
        seed["records"],
        scores,
        previous_digest=first,
        generated_at=NOW + timedelta(hours=1),
        demo=True,
    )
    assert "New since last: 0 listing(s)" in second


def test_new_since_last_uses_last_seen_after_prior_render():
    seed = seed_data()
    first_scores = score_records(seed["records"], seed["profile"], NOW, cache=False, model_url="")
    first = render_digest(seed["records"], first_scores, generated_at=NOW, demo=True)
    seed["records"][0]["last_seen_at"] = (NOW + timedelta(minutes=1)).isoformat()
    updated_scores = score_records(
        seed["records"], seed["profile"], NOW + timedelta(minutes=1), cache=False, model_url=""
    )
    second = render_digest(
        seed["records"],
        updated_scores,
        previous_digest=first,
        generated_at=NOW + timedelta(minutes=2),
        demo=True,
    )
    assert "New since last: 1 listing(s)" in second


def test_suppressed_count_includes_dedupe_losers_once():
    seed = seed_data()
    seed["records"][1]["superseded_by"] = seed["records"][0]["id"]
    scores = score_records(seed["records"], seed["profile"], NOW, cache=False, model_url="")
    text = render_digest(
        seed["records"],
        scores,
        suppressed_count=3,
        generated_at=NOW,
        demo=True,
    )
    assert "Suppressed count: 5" in text


def test_top_eight_are_selected_before_alphabetical_source_grouping():
    records = [
        {
            "id": f"id-{index}",
            "source": "reddit" if index % 2 else "locanto",
            "title": f"Listing {index}",
            "url": f"https://example.invalid/{index}",
            "age": 30,
            "distance_miles": index,
            "posted_at": NOW.isoformat(),
        }
        for index in range(10)
    ]
    scores = [
        {"id": record["id"], "score": 100 - index, "why": "Matches criteria. Relevant signals were present."}
        for index, record in enumerate(records)
    ]
    text = render_digest(records, scores, threshold=90, limit=3, generated_at=NOW)
    assert "at most 3 listings" in text
    assert "ID: id-0" in text
    assert "ID: id-1" in text
    assert "ID: id-2" in text
    assert "ID: id-3" not in text
    assert text.index("locanto (") < text.index("reddit (")
