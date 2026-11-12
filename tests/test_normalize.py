from datetime import datetime, timezone
import hashlib

import pytest

from src.normalize import extract_age, normalize_record


NOW = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)


@pytest.fixture
def basic_listing():
    return {
        "source": "reddit",
        "title": "Looking to meet",
        "url": "https://www.reddit.com/r/r4r/comments/abc/meet/?utm_source=test#comments",
        "text_excerpt": "Age: 32. Open to meeting this week.",
        "posted_at": "2026-11-12T20:00:00Z",
    }


@pytest.fixture
def bracket_tagged_listing():
    return {
        "source": "reddit",
        "title": "[29][M4F][Salt Lake City] looking for a connection",
        "url": "https://www.reddit.com/r/SLCr4r/comments/def/post/",
        "text_excerpt": "Athletic build, about 4.5 miles away.",
        "last_seen_at": "2026-11-12T20:30:00-07:00",
    }


@pytest.fixture
def sparse_listing():
    return {
        "source": "locanto",
        "title": "A friendly introduction",
        "url": "https://example.com/ad/123",
        "body": "Let's chat and see if we get along.",
    }


@pytest.fixture
def minor_listing():
    return {
        "source": "reddit",
        "title": "Age: 17, looking to meet",
        "url": "https://www.reddit.com/r/r4r/comments/minor/post/",
        "body": "A casual introduction.",
    }


@pytest.fixture
def labeled_location_listing():
    return {
        "source": "squirt",
        "title": "A local meetup",
        "url": "https://squirt.org/listings/456",
        "body": "Location: West Valley City\n42 years old. Muscular and toned.",
        "posted": "2 hours ago",
        "photos": ["https://images.example/photo.jpg?utm_campaign=feed"],
        "distance_miles": "3.2",
    }


@pytest.fixture
def nonconsensual_listing():
    return {
        "source": "doublelist",
        "title": "A non-consensual encounter",
        "url": "https://example.com/ad/unsafe",
        "body": "This listing explicitly describes non-consensual activity.",
    }


def test_basic_fields_and_stable_sha256_id(basic_listing):
    record = normalize_record(basic_listing, NOW)
    expected_url = "https://www.reddit.com/r/r4r/comments/abc/meet/"
    assert record["id"] == hashlib.sha256(f"reddit{expected_url}".encode()).hexdigest()
    assert record["source"] == "reddit"
    assert record["url"] == expected_url
    assert record["age"] == 32
    assert record["posted_at"] == "2026-11-12T20:00:00+00:00"
    assert record["last_seen_at"] == "2026-11-12T21:00:00+00:00"
    assert record["raw"] == basic_listing


def test_r4r_bracket_age_location_and_body_type(bracket_tagged_listing):
    record = normalize_record(bracket_tagged_listing, NOW)
    assert record["age"] == 29
    assert record["location"] == "Salt Lake City"
    assert record["distance_miles"] == 4.5
    assert "body_type:athletic" in record["tags"]
    assert record["last_seen_at"] == "2026-11-13T03:30:00+00:00"


def test_missing_values_remain_null(sparse_listing):
    record = normalize_record(sparse_listing, NOW)
    assert record["age"] is None
    assert record["location"] is None
    assert record["distance_miles"] is None
    assert record["posted_at"] is None
    assert record["photos"] == []


def test_underage_listing_is_suppressed(minor_listing):
    assert extract_age(minor_listing["title"]) == 17
    assert normalize_record(minor_listing, NOW) is None
    zero_age = {**minor_listing, "title": "Age: 0"}
    assert normalize_record(zero_age, NOW) is None
    adult = {**minor_listing, "title": "Age: 18", "url": "https://example.com/ad/adult"}
    assert normalize_record(adult, NOW)["age"] == 18


def test_labeled_location_body_tags_photos_and_relative_time(labeled_location_listing):
    record = normalize_record(labeled_location_listing, NOW)
    assert record["age"] == 42
    assert record["location"] == "West Valley City"
    assert record["distance_miles"] == 3.2
    assert record["posted_at"] == "2026-11-12T19:00:00+00:00"
    assert record["photos"] == ["https://images.example/photo.jpg"]
    assert "body_type:muscular" in record["tags"]
    assert "body_type:toned" in record["tags"]


def test_nonconsensual_listing_is_suppressed(nonconsensual_listing):
    assert normalize_record(nonconsensual_listing, NOW) is None
