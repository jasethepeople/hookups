from __future__ import annotations

import re
import sqlite3

from fastapi.testclient import TestClient

import src.web as web


def _token(page: str) -> str:
    return re.search(r'<meta name="action-token" content="([^"]+)">', page).group(1)


def _client(monkeypatch, tmp_path):
    db_path = tmp_path / "dashboard.sqlite3"
    monkeypatch.setattr(web, "DB_PATH", db_path)
    monkeypatch.setattr(web, "DEMO_MODE", True)
    monkeypatch.setattr(web, "load_profile", lambda: {"recent_openers": []})
    assert web.seed_demo_data(db_path) == 10
    return TestClient(web.app), db_path


def test_demo_seed_is_ten_rows_and_idempotent(monkeypatch, tmp_path):
    client, db_path = _client(monkeypatch, tmp_path)
    assert web.seed_demo_data(db_path) == 10
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM listings WHERE source_id LIKE 'demo-%'").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM score_cache").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM listings WHERE source_id LIKE 'demo-%' AND thumb_url IS NOT NULL").fetchone()[0] == 7
    client.close()


def test_dashboard_filters_sorted_feed_and_disables_unconfigured_opener(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert "DEMO DATA · 10 SYNTHETIC LISTINGS" in page.text
        assert page.text.count('<article class="card"') == 10
        assert 'disabled title="Add one of your own recent openers' in page.text
        locanto = client.get("/?source=locanto&min_score=0")
        assert locanto.status_code == 200
        assert locanto.text.count('<article class="card"') == 3
        high_score = client.get("/?min_score=70")
        assert high_score.status_code == 200
        high_scores = [int(value) for value in re.findall(r'<div class="score">(\d+)<small>', high_score.text)]
        assert high_scores and all(value >= 70 for value in high_scores)
        assert len(high_scores) < 10
        scores = web.feed_records(web.DB_PATH)
        expected = sorted(scores, key=lambda item: (-item["score"], -web._timestamp_sort(item["last_seen_at"])))
        assert scores == expected


def test_seen_hide_and_block_domain_actions_are_private_and_persist(monkeypatch, tmp_path):
    client, db_path = _client(monkeypatch, tmp_path)
    with client:
        page = client.get("/")
        token = _token(page.text)
        assert client.post("/api/listings/1/seen", headers={"X-Action-Token": token}).json()["status"] == "seen"
        assert client.get("/api/listings/1/hide").status_code == 405
        assert client.post("/api/listings/1/hide").status_code == 403
        page = client.get("/")
        assert "seen" in page.text
        assert client.post("/api/listings/1/hide", headers={"X-Action-Token": _token(page.text)}).json()["status"] == "hidden"
        assert 'id="listing-1"' not in client.get("/").text
        with sqlite3.connect(db_path) as connection:
            row = connection.execute("SELECT id FROM listings WHERE source_id = 'demo-02'").fetchone()
        page = client.get("/")
        result = client.post(f"/api/listings/{row[0]}/block-domain", headers={"X-Action-Token": _token(page.text)})
        assert result.json()["blocked_domain"] == "doublelist.demo.invalid"
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM blocked_domains WHERE domain = 'doublelist.demo.invalid'").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM listings WHERE source = 'doublelist' AND review_status = 'hidden'").fetchone()[0] == 3
    client.close()
