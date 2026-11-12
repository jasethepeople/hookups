from datetime import datetime

import pytest

from src.dedupe import BODY_OVERLAP_THRESHOLD, dedupe_records, trigram_jaccard


@pytest.fixture
def identical_photo_cross_post():
    return [
        {
            "id": "locanto-old",
            "source": "locanto",
            "url": "https://locanto.example/ad/old",
            "photos": ["https://images.example/shared.jpg"],
            "age": None,
            "distance_miles": None,
            "body": "A short older listing.",
            "posted_at": "2026-11-11T10:00:00Z",
        },
        {
            "id": "reddit-new",
            "source": "reddit",
            "url": "https://reddit.example/post/new",
            "photos": ["https://images.example/shared.jpg"],
            "age": 31,
            "distance_miles": 4.0,
            "body": "A different description.",
            "posted_at": "2026-11-12T10:00:00Z",
        },
    ]


@pytest.fixture
def matching_body_cross_post():
    shared = (
        "I enjoy coffee, hiking, live music, classic films, cooking, weekend drives, "
        "old bookstores, interesting conversations, snowy mornings, and warm summer evenings. "
    )
    body = "Looking for a relaxed connection with someone kind and thoughtful."
    return [
        {
            "id": "doublelist-older",
            "source": "doublelist",
            "url": "https://doublelist.example/ad/older",
            "photos": [],
            "age": 29,
            "distance_miles": 3.2,
            "body": shared + body,
            "posted_at": "2026-11-12T08:00:00Z",
        },
        {
            "id": "reddit-newer",
            "source": "reddit",
            "url": "https://reddit.example/post/newer",
            "photos": [],
            "age": 29,
            "distance_miles": 4.9,
            "body": shared + body + " Maybe we can start with coffee.",
            "posted_at": "2026-11-12T20:00:00+00:00",
        },
    ]


@pytest.fixture
def near_miss_cross_post():
    shared = (
        "I enjoy coffee, hiking, live music, classic films, cooking, weekend drives, "
        "old bookstores, interesting conversations, snowy mornings, and warm summer evenings. "
    )
    return [
        {
            "id": "squirt-a",
            "source": "squirt",
            "url": "https://squirt.example/ad/a",
            "photos": [],
            "age": 34,
            "distance_miles": 4.0,
            "body": shared + "Looking for a relaxed connection with someone kind and thoughtful.",
            "posted_at": "2026-11-12T08:00:00Z",
        },
        {
            "id": "reddit-b",
            "source": "reddit",
            "url": "https://reddit.example/post/b",
            "photos": [],
            "age": 34,
            "distance_miles": 4.5,
            "body": shared + "Hoping to meet someone for a lively, intense, high-energy adventure.",
            "posted_at": "2026-11-12T12:00:00Z",
        },
    ]


@pytest.fixture
def stale_body_cross_post(matching_body_cross_post):
    older, newer = matching_body_cross_post
    older = {**older, "id": "stale-old", "posted_at": "2026-11-11T18:00:00Z"}
    newer = {**newer, "id": "stale-new", "posted_at": "2026-11-12T20:01:00Z"}
    return [older, newer]


def test_identical_photo_cross_post_supersedes_older_record(identical_photo_cross_post):
    records = identical_photo_cross_post
    result = dedupe_records(records)
    assert result is records
    assert records[0]["superseded_by"] == "reddit-new"
    assert records[1]["superseded_by"] is None
    same_source = [dict(records[0]), {**records[1], "source": "locanto"}]
    assert all(item["superseded_by"] is None for item in dedupe_records(same_source))


def test_body_overlap_requires_matching_age_bucket_and_recent_post(matching_body_cross_post):
    records = matching_body_cross_post
    assert trigram_jaccard(records[0]["body"], records[1]["body"]) >= BODY_OVERLAP_THRESHOLD
    dedupe_records(records)
    assert records[0]["superseded_by"] == "reddit-newer"
    assert records[1]["superseded_by"] is None
    age_mismatch = [{**records[0], "superseded_by": None}, {**records[1], "age": 30, "superseded_by": None}]
    distance_mismatch = [{**records[0], "superseded_by": None}, {**records[1], "distance_miles": 5.0, "superseded_by": None}]
    assert all(item["superseded_by"] is None for item in dedupe_records(age_mismatch))
    assert all(item["superseded_by"] is None for item in dedupe_records(distance_mismatch))


def test_near_miss_below_eighty_percent_is_not_merged(near_miss_cross_post):
    records = near_miss_cross_post
    assert trigram_jaccard(records[0]["body"], records[1]["body"]) < BODY_OVERLAP_THRESHOLD
    assert all(item["superseded_by"] is None for item in dedupe_records(records))


def test_body_match_outside_twenty_four_hours_is_not_merged(stale_body_cross_post):
    records = stale_body_cross_post
    assert trigram_jaccard(records[0]["body"], records[1]["body"]) >= BODY_OVERLAP_THRESHOLD
    delta = abs(
        datetime.fromisoformat(records[0]["posted_at"].replace("Z", "+00:00"))
        - datetime.fromisoformat(records[1]["posted_at"].replace("Z", "+00:00"))
    )
    assert delta.total_seconds() > 24 * 60 * 60
    assert all(item["superseded_by"] is None for item in dedupe_records(records))
