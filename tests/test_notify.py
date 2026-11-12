from pathlib import Path

import pytest

from src.notify import chunk_text, handle_update, is_hookups_command, read_settings


def _message(chat_id=12345, text="/hookups", chat_type="private", is_bot=False):
    return {
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"is_bot": is_bot},
        }
    }


def test_chunk_text_stays_below_telegram_utf16_limit():
    chunks = chunk_text(("a" * 3799 + "😀") * 2)
    assert len(chunks) == 3
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3800 for chunk in chunks)
    assert "".join(chunks) == ("a" * 3799 + "😀") * 2


def test_read_settings_keeps_blank_credentials_blank(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_TOKEN=\nTELEGRAM_CHAT_ID=\nMODEL_URL=\n", encoding="utf-8")
    assert read_settings(env_path) == {"token": "", "chat_id": "", "model_url": ""}


def test_hookups_command_requires_the_configured_private_chat():
    assert is_hookups_command(_message(text="/hookups@mybot now"), "12345")
    assert not is_hookups_command(_message(chat_id=12346), "12345")
    assert not is_hookups_command(_message(chat_type="group"), "12345")
    assert not is_hookups_command(_message(is_bot=True), "12345")
    assert not is_hookups_command(_message(text="/hookups-extra"), "12345")


def test_handle_update_renders_and_sends_only_authorized_command():
    calls = []
    renderer = lambda **kwargs: "private digest"
    sender = lambda token, chat_id, text: calls.append((token, chat_id, text)) or 1
    assert handle_update(_message(), "token", "12345", renderer=renderer, sender=sender)
    assert calls == [("token", "12345", "private digest")]
    assert not handle_update(_message(chat_id=99), "token", "12345", renderer=renderer, sender=sender)
    assert len(calls) == 1


def test_blank_telegram_configuration_never_sends_or_polls(tmp_path, monkeypatch):
    import src.notify as notify

    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_TOKEN=\nTELEGRAM_CHAT_ID=\nMODEL_URL=\n", encoding="utf-8")
    monkeypatch.setattr(notify, "render_live_digest", lambda *args, **kwargs: "dry digest\n")
    monkeypatch.setattr(notify, "_telegram_call", lambda *args, **kwargs: pytest.fail("network call in dry mode"))
    assert notify.main(["--env", str(env_path), "--dry"]) == 0


def test_empty_digest_has_no_chunks():
    assert chunk_text("") == []
