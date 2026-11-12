from datetime import datetime, timedelta, timezone
import sqlite3

import httpx

import src.score as score_module
from src.score import score_records, score_record


NOW = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)
PROFILE = {
    "max_drive_minutes": 20,
    "age_range": {"min_age": 25, "max_age": 40},
    "body_types": ["athletic"],
    "positions": ["versatile"],
    "must_haves": ["kind"],
    "host_or_travel": "host",
    "discretion_level": "discreet",
    "dealbreakers": ["smoke"],
}


def seeded_rows():
    return [
        {
            "id": "seed-1",
            "source": "reddit",
            "title": "34, athletic versatile, kind",
            "url": "https://example.invalid/1",
            "body": "Can host. Discreet.",
            "photos": ["https://images.example.invalid/1.jpg"],
            "age": 34,
            "distance_miles": 0,
            "tags": ["body_type:athletic", "position:versatile", "host", "discreet"],
            "posted_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "raw": {},
        },
        {
            "id": "seed-2",
            "source": "locanto",
            "title": "30, athletic, kind",
            "url": "https://example.invalid/2",
            "body": "Friendly and kind.",
            "photos": [],
            "age": 30,
            "distance_miles": 4,
            "tags": ["body_type:athletic"],
            "posted_at": NOW.isoformat(),
            "last_seen_at": (NOW - timedelta(hours=36)).isoformat(),
            "raw": {},
        },
        {
            "id": "seed-3",
            "source": "doublelist",
            "title": "45, athletic versatile, kind",
            "url": "https://example.invalid/3",
            "body": "Can host. Discreet.",
            "photos": ["https://images.example.invalid/3.jpg"],
            "age": 45,
            "distance_miles": 12,
            "tags": ["body_type:athletic", "position:versatile", "host", "discreet"],
            "posted_at": NOW.isoformat(),
            "last_seen_at": (NOW - timedelta(hours=90)).isoformat(),
            "raw": {},
        },
        {
            "id": "seed-4",
            "source": "squirt",
            "title": "28, athletic versatile, kind",
            "url": "https://example.invalid/4",
            "body": "I smoke. Can host. Discreet.",
            "photos": [],
            "age": 28,
            "distance_miles": 2.5,
            "tags": ["body_type:athletic", "position:versatile", "host", "discreet"],
            "posted_at": NOW.isoformat(),
            "last_seen_at": (NOW - timedelta(hours=6)).isoformat(),
            "raw": {},
        },
        {
            "id": "seed-5",
            "source": "reddit",
            "title": "Details unavailable",
            "url": "https://example.invalid/5",
            "body": "",
            "photos": [],
            "age": None,
            "distance_miles": None,
            "tags": [],
            "posted_at": None,
            "last_seen_at": None,
            "raw": {},
        },
    ]


def test_five_seeded_rows_print_score_table_and_respect_weights():
    rows = seeded_rows()
    results = score_records(rows, PROFILE, NOW, cache=False, model_url="")
    expected = [100, 48, 40, 0, 0]
    assert [result["score"] for result in results] == expected
    assert all(0 <= result["score"] <= 100 for result in results)
    assert all(sum(result["components"].values()) <= 100 for result in results)
    assert all(len(result["why"].split(". ")) == 2 for result in results)
    assert not any(result["model_used"] for result in results)
    print("ID      SCORE  DIST  SEEN  AGE  BODY/POS  HOST  PHOTO  DISCRETION  WHY")
    for result in results:
        components = result["components"]
        print(
            f"{result['id']:<7} {result['score']:>5}  "
            f"{components['distance']:>4}  {components['last_seen']:>4}  "
            f"{components['age']:>3}  {components['body_position']:>8}  "
            f"{components['hosts']:>4}  {components['photos']:>5}  "
            f"{components['discretion']:>10}  {result['why']}"
        )


def test_dealbreaker_hard_zeros_and_model_failure_falls_back(monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("model unavailable")

    monkeypatch.setattr(score_module.httpx, "post", unavailable)
    result = score_record(
        seeded_rows()[3],
        PROFILE,
        NOW,
        cache=False,
        model_url="http://model.invalid/score",
    )
    assert result["score"] == 0
    assert result["hard_zero"] is True
    assert result["model_used"] is False
    assert result["why"] == (
        "A configured dealbreaker matched, so the score is 0/100. "
        "Positive signals cannot override a hard exclusion."
    )


def test_unavailable_model_uses_deterministic_explanation(monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("model unavailable")

    monkeypatch.setattr(score_module.httpx, "post", unavailable)
    result = score_record(
        seeded_rows()[0],
        PROFILE,
        NOW,
        cache=False,
        model_url="http://model.invalid/score",
    )
    assert result["score"] == 100
    assert result["model_used"] is False
    assert len(result["why"].split(". ")) == 2


def test_model_returns_only_validated_two_sentence_why(monkeypatch):
    calls = []

    def respond(url, json, timeout, follow_redirects):
        calls.append((url, json, timeout, follow_redirects))
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"output": "The profile matches several explicit preferences. Distance and recency also help."},
            request=request,
        )

    monkeypatch.setattr(score_module.httpx, "post", respond)
    result = score_record(
        seeded_rows()[0],
        PROFILE,
        NOW,
        cache=False,
        model_url="https://model.example.invalid/score",
    )
    assert len(calls) == 1
    assert calls[0][1]["input"]
    assert result["model_used"] is True
    assert result["why"] == "The profile matches several explicit preferences. Distance and recency also help."


def test_score_cache_is_by_record_id_and_input_hash(tmp_path, monkeypatch):
    calls = []

    def respond(url, json, timeout, follow_redirects):
        calls.append(url)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"output": "The listing matches stated criteria. Its score reflects the available signals."}, request=request)

    monkeypatch.setattr(score_module.httpx, "post", respond)
    db_path = tmp_path / "score.sqlite3"
    model_url = "https://model.example.invalid/score?token=do-not-store"
    record = seeded_rows()[0]
    first = score_record(record, PROFILE, NOW, db_path, True, model_url)
    second = score_record(record, PROFILE, NOW, db_path, True, model_url)
    changed_profile = {**PROFILE, "max_drive_minutes": 40}
    third = score_record(record, changed_profile, NOW, db_path, True, model_url)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False
    assert len(calls) == 2
    connection = sqlite3.connect(db_path)
    try:
        cached = connection.execute("SELECT record_id, input_hash, score, model_used FROM score_cache").fetchall()
    finally:
        connection.close()
    assert len(cached) == 1
    assert cached[0][0] == "seed-1"
    assert cached[0][3] == 1
    assert b"do-not-store" not in db_path.read_bytes()
