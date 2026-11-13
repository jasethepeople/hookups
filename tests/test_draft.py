from datetime import datetime, timezone
import sqlite3
import re

from src.draft import _self_stats, draft_variants, edit_draft, generate_drafts, skip_draft
from src.web import seed_demo_data


NOW = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)


def _sentences(text):
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def test_grindr_variants_are_stats_forward_and_at_most_ten_words():
    record = {"source": "grindr-via-notification-bridge", "title": "Easygoing coffee and conversation"}
    variants = draft_variants(record, ["Age 37", "versatile", "Demo District"])
    assert len(variants) == 2
    for variant in variants:
        assert len(variant.split()) <= 10
        assert variant.startswith("Age 37")


def test_doublelist_variants_have_stats_specific_hook_and_three_or_four_sentences():
    record = {"source": "doublelist", "title": "Thoughtful company, no rush"}
    variants = draft_variants(record, ["Age 37", "athletic", "versatile"])
    assert len(variants) == 2
    for variant in variants:
        assert 3 <= len(_sentences(variant)) <= 4
        assert "Age 37, athletic, versatile" in variant
        assert "Thoughtful company, no rush" in variant


def test_reddit_variants_are_short_comments_with_listing_hook():
    record = {"source": "reddit", "title": "Looking for a warm, low-key connection"}
    variants = draft_variants(record, [])
    assert len(variants) == 2
    for variant in variants:
        assert len(variant.split()) <= 18
        assert "Looking for a warm, low-key connection" in variant


def test_match_preferences_are_not_reused_as_personal_stats():
    assert _self_stats({"body_types": ["athletic"], "positions": ["top"]}) == []
    assert _self_stats({"self_stats": {"age": 37, "position": "versatile"}}) == ["Age 37", "versatile"]


def test_three_seeded_listings_store_two_idempotent_variants(tmp_path):
    db_path = tmp_path / "seeded.sqlite3"
    seed_demo_data(db_path, now=NOW)
    profile = {"self_stats": {}, "age_range": {"min_age": None, "max_age": None}, "max_drive_minutes": None}
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()[0]
    first = generate_drafts(db_path, profile=profile, now=NOW, limit=3)
    with sqlite3.connect(db_path) as connection:
        after_first = connection.execute("SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()[0]
    second = generate_drafts(db_path, profile=profile, now=NOW, limit=3)
    with sqlite3.connect(db_path) as connection:
        after_second = connection.execute("SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()[0]
    assert len(first) == len(second) == 3
    assert all(len(item["variants"]) == 2 for item in first)
    assert all(item["source"] not in {"doublelist", "grindr", "grindr-via-notification-bridge"} for item in first)
    assert [(item["listing_id"], item["variants"]) for item in first] == [(item["listing_id"], item["variants"]) for item in second]
    assert after_first >= before
    assert after_second == after_first


def test_regeneration_does_not_restore_edited_or_skipped_drafts(tmp_path):
    db_path = tmp_path / "draft-state.sqlite3"
    seed_demo_data(db_path, now=NOW)
    profile = {"self_stats": {}, "age_range": {"min_age": None, "max_age": None}, "max_drive_minutes": None}
    with sqlite3.connect(db_path) as connection:
        listing_id, first_id, second_id = connection.execute(
            "SELECT listing_id, MIN(id), MAX(id) FROM drafts WHERE status = 'draft' GROUP BY listing_id ORDER BY listing_id LIMIT 1"
        ).fetchone()
    edit_draft(first_id, "Edited by the user.", db_path, now=NOW)
    skip_draft(second_id, db_path, now=NOW)
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM drafts WHERE listing_id = ?", (listing_id,)).fetchone()[0]
    generate_drafts(db_path, profile=profile, now=NOW, limit=3)
    with sqlite3.connect(db_path) as connection:
        after = connection.execute("SELECT COUNT(*) FROM drafts WHERE listing_id = ?", (listing_id,)).fetchone()[0]
        rows = connection.execute("SELECT id, status, body FROM drafts WHERE listing_id = ? ORDER BY id", (listing_id,)).fetchall()
    assert before == after == 2
    assert [row[1] for row in rows] == ["draft", "discarded"]
    assert "Edited by the user." in [row[2] for row in rows]
