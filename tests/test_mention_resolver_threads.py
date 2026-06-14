"""Tests for the mention-resolver's thread orientation note and that it
still resolves @mentions. channel_parent_id is monkeypatched -- no network."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import discord_mention_resolver as mr  # noqa: E402

TAG = ('<channel source="plugin:discord:discord" chat_id="555" '
       'message_id="9" user="alice" user_id="123">')


def test_chat_id_extracted_from_tag():
    assert mr._chat_id_from_payload(TAG + "hi</channel>") == "555"


def test_chat_id_none_without_tag():
    assert mr._chat_id_from_payload("just text") is None


def test_thread_note_present_for_thread(monkeypatch):
    import narrate
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: "PARENT")
    note = mr._thread_note("555")
    assert note and "thread" in note.lower()
    assert "PARENT" in note and "555" in note


def test_thread_note_none_for_plain_channel(monkeypatch):
    import narrate
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: None)
    assert mr._thread_note("555") is None


def test_thread_note_swallows_errors(monkeypatch):
    import narrate
    monkeypatch.setattr(narrate, "channel_parent_id",
                        lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mr._thread_note("555") is None


def test_main_emits_thread_note(monkeypatch, capsys):
    import narrate
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: "PARENT")
    monkeypatch.setattr(sys, "stdin", _Stdin(TAG + "hello</channel>"))
    assert mr.main() == 0
    out = capsys.readouterr().out
    assert "thread" in out.lower() and "PARENT" in out


def test_main_mentions_still_resolve(tmp_path, monkeypatch, capsys):
    import narrate
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: None)
    roster = tmp_path / "roster.json"
    roster.write_text('{"123": "Alice"}')
    monkeypatch.setenv("CCDK_DISCORD_ROSTER", str(roster))
    payload = TAG + "hey <@123></channel>"
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    assert mr.main() == 0
    out = capsys.readouterr().out
    assert "mentions resolved" in out.lower()
    assert "Alice" in out


class _Stdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
