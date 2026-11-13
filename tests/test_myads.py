from datetime import datetime, timezone

import pytest

from src.myads import generate_ads, render_markdown


def test_generates_exact_variant_counts_without_inventing_profile_facts():
    ads = generate_ads({"self_stats": {"age": None, "body_type": None, "position": None, "location": None, "host_or_travel": None}})

    assert len(ads["locanto"]["headlines"]) == 3
    assert len(ads["locanto"]["bodies"]) == 2
    assert len(ads["doublelist"]["headlines"]) == 3
    assert len(ads["doublelist"]["bodies"]) == 2
    all_copy = " ".join(ads["locanto"]["bodies"] + ads["doublelist"]["bodies"])
    assert "My stats:" not in all_copy
    assert "Age " not in all_copy
    assert "Salt Lake" not in all_copy


def test_uses_only_explicit_self_stats_not_match_preferences():
    profile = {
        "self_stats": {"age": 36, "body_type": "athletic", "position": None, "location": "Salt Lake City", "host_or_travel": None},
        "body_types": ["bear"],
        "positions": ["top"],
        "home_zip": "84101",
    }

    ads = generate_ads(profile)
    all_copy = " ".join(ads["locanto"]["bodies"] + ads["doublelist"]["bodies"])

    assert "age 36" in all_copy
    assert "body type: athletic" in all_copy
    assert "area: Salt Lake City" in all_copy
    assert "bear" not in all_copy
    assert "top" not in all_copy
    assert "84101" not in all_copy


def test_render_includes_platform_rules_and_photo_checklist():
    text = render_markdown({}, datetime(2026, 11, 13, 12, tzinfo=timezone.utc))

    assert text.count("### Headlines") == 2
    assert text.count("### Body variants") == 2
    assert "Doublelist Community Guidelines" in text
    assert "Locanto Safety Center" in text
    assert "## Photo checklist" in text
    assert "Nothing is posted or sent" in text
    assert "No `self_stats` are set" in text


def test_underage_profile_age_is_rejected():
    with pytest.raises(ValueError, match="adult-only"):
        generate_ads({"self_stats": {"age": 17}})
