from __future__ import annotations

from datetime import datetime, timezone
import re
import sqlite3

from fastapi.testclient import TestClient

from src import notify
from src.draft import approve_draft, edit_draft, generate_drafts, log_contact, skip_draft
from src.web import seed_demo_data
import src.web as web


NOW = datetime(2026, 11, 12, 23, 10, tzinfo=timezone.utc)


def _seed(db_path):
    seed_demo_data(db_path, now=NOW)
    profile = {"self_stats": {}, "age_range": {"min_age": None, "max_age": None}, "max_drive_minutes": None}
    generate_drafts(db_path, profile=profile, now=NOW, limit=3)
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT MIN(d.id), d.listing_id, l.canonical_url FROM drafts AS d JOIN listings AS l ON l.id = d.listing_id "
            "WHERE d.status = 'draft' GROUP BY d.listing_id ORDER BY d.listing_id LIMIT 3"
        ).fetchall()


def test_approve_edit_skip_and_log_flow_on_seed_data(tmp_path):
    db_path = tmp_path / "approval.sqlite3"
    targets = _seed(db_path)
    assert len(targets) == 3
    approve_id, approve_listing, _ = targets[0]
    edit_id, edit_listing, edit_url = targets[1]
    skip_id, skip_listing, _ = targets[2]

    approved_text = approve_draft(approve_id, db_path, now=NOW)
    assert approved_text
    edit_draft(edit_id, "Edited with only the known listing hook.", db_path, now=NOW)
    skip_draft(skip_id, db_path, now=NOW)
    logged_listing_id = log_contact(edit_url, "replied", db_path, now=NOW)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT id, listing_id, status, ts FROM contacts ORDER BY listing_id, id"
        ).fetchall()
        draft_states = connection.execute(
            "SELECT id, listing_id, status, body FROM drafts WHERE id IN (?, ?, ?) ORDER BY id",
            (approve_id, edit_id, skip_id),
        ).fetchall()
    assert rows[0][1:3] == (approve_listing, "sent")
    assert rows[1][1:3] == (edit_listing, "replied")
    assert rows[1][3] == NOW.isoformat(timespec="seconds")
    assert logged_listing_id == edit_listing
    assert [row[1] for row in draft_states] == [approve_listing, edit_listing, skip_listing]
    assert [row[2] for row in draft_states] == ["approved", "draft", "discarded"]
    assert draft_states[1][3] == "Edited with only the known listing hook."


def test_telegram_log_command_is_private_and_updates_outcome(tmp_path):
    db_path = tmp_path / "telegram-log.sqlite3"
    targets = _seed(db_path)
    url = targets[0][2]
    sent = []
    update = {
        "message": {
            "chat": {"id": 12345, "type": "private"},
            "from": {"is_bot": False},
            "text": f"/log {url} met",
        }
    }
    assert notify.handle_update(
        update,
        "unused-token",
        "12345",
        db_path=db_path,
        sender=lambda token, chat_id, text: sent.append(text) or 1,
    )
    assert sent and "Logged met" in sent[0]
    assert not notify.handle_update(
        update,
        "unused-token",
        "99999",
        db_path=db_path,
        sender=lambda token, chat_id, text: (_ for _ in ()).throw(AssertionError("must not respond to another chat")),
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM contacts WHERE listing_id = ?", (targets[0][1],)).fetchone()[0] == "met"


def test_dashboard_approval_requires_action_token_and_records_sent(monkeypatch, tmp_path):
    db_path = tmp_path / "dashboard-approval.sqlite3"
    monkeypatch.setattr(web, "DB_PATH", db_path)
    monkeypatch.setattr(web, "DEMO_MODE", True)
    monkeypatch.setattr(web, "load_profile", lambda: {"recent_openers": []})
    with TestClient(web.app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "navigator.clipboard.writeText(textarea.value)" in page.text
        match = re.search(r'<meta name="action-token" content="([^"]+)">', page.text)
        assert match
        with sqlite3.connect(db_path) as connection:
            draft_id, listing_id = connection.execute(
                "SELECT d.id, d.listing_id FROM drafts AS d JOIN listings AS l ON l.id = d.listing_id "
                "WHERE d.status = 'draft' ORDER BY d.id LIMIT 1"
            ).fetchone()
        assert client.post(f"/api/drafts/{draft_id}/approve").status_code == 403
        response = client.post(
            f"/api/drafts/{draft_id}/approve",
            headers={"X-Action-Token": match.group(1)},
        )
        assert response.status_code == 200
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT status, ts FROM contacts WHERE listing_id = ?", (listing_id,)
            ).fetchone()[0] == "sent"
