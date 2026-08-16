import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import llm, messages, notify
from app.config import settings
from app.main import app
from app.messages import Extracted, Message

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_voice_dials_cell_first():
    r = client.post("/voice")
    assert r.status_code == 200
    body = r.text
    assert "<Dial" in body
    assert f'timeout="{settings.dial_timeout}"' in body
    assert settings.forward_to_number in body
    assert 'action="/voice/after-dial"' in body


def test_after_dial_completed_hangs_up():
    r = client.post("/voice/after-dial", data={"DialCallStatus": "completed"})
    assert r.status_code == 200
    assert "<Hangup/>" in r.text
    assert "<Say>" not in r.text


def test_after_dial_no_answer_connects_relay():
    r = client.post("/voice/after-dial", data={"DialCallStatus": "no-answer"})
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text
    assert 'url="wss://' in r.text
    assert 'action="/voice/handoff"' in r.text
    assert f'ttsProvider="{settings.tts_provider}"' in r.text
    assert f'voice="{settings.tts_voice}"' in r.text


def test_handoff_hangs_up():
    r = client.post("/voice/handoff")
    assert r.status_code == 200
    assert "<Hangup/>" in r.text


def _stub_capture_and_notify(monkeypatch):
    """Keep the disconnect/finalize path offline. Returns the sent-SMS list."""
    sent: list[str] = []

    async def fake_capture(history, call_sid, from_number):
        return None

    async def fake_send_sms(body):
        sent.append(body)

    monkeypatch.setattr(messages, "capture", fake_capture)
    monkeypatch.setattr(notify, "send_sms", fake_send_sms)
    return sent


def test_ws_streams_llm_reply(monkeypatch):
    _stub_capture_and_notify(monkeypatch)

    async def fake_stream(system, messages_):
        assert messages_[-1] == {"role": "user", "content": "hi there"}
        for token in ["Hello! ", "Who's ", "calling?"]:
            yield token

    monkeypatch.setattr(llm, "stream_reply", fake_stream)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json({"type": "prompt", "voicePrompt": "hi there"})

        tokens = []
        while True:
            msg = ws.receive_json()
            tokens.append(msg["token"])
            if msg["last"]:
                break

    assert tokens[-1] == ""  # empty end-of-turn marker
    assert "".join(tokens) == "Hello! Who's calling?"


def test_ws_bot_ends_call(monkeypatch):
    _stub_capture_and_notify(monkeypatch)

    async def fake_stream(system, messages_):
        for token in ["Thanks! ", "Goodbye.", "[[END]]"]:
            yield token

    monkeypatch.setattr(llm, "stream_reply", fake_stream)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json({"type": "prompt", "voicePrompt": "bye"})

        spoken = []
        while True:
            msg = ws.receive_json()
            if msg["type"] == "text":
                spoken.append(msg["token"])
                if msg["last"]:
                    break

        end = ws.receive_json()

    # The marker is stripped — never spoken to the caller.
    assert "".join(spoken) == "Thanks! Goodbye."
    assert "[[END]]" not in "".join(spoken)
    # And the bot signals Twilio to end the call.
    assert end["type"] == "end-session"


def test_capture_assembles_message(monkeypatch):
    async def fake_extract(system, user, schema):
        assert "Caller:" in user  # transcript was rendered
        return Extracted(
            caller_name="Jane Doe",
            callback_number="+15035550134",
            reason="Saturday pickup",
            urgency="normal",
        )

    monkeypatch.setattr(llm, "extract", fake_extract)

    history = [
        {"role": "assistant", "content": "Who's calling?"},
        {"role": "user", "content": "Jane, about Saturday's pickup"},
    ]
    msg = asyncio.run(messages.capture(history, "CA9", "+15035550134"))

    assert isinstance(msg, Message)
    assert msg.caller_name == "Jane Doe"
    assert msg.reason == "Saturday pickup"
    assert msg.from_number == "+15035550134"
    assert msg.call_sid == "CA9"
    assert "Jane, about Saturday's pickup" in msg.transcript


def test_capture_none_without_caller_turns():
    history = [{"role": "assistant", "content": "Who's calling?"}]
    assert asyncio.run(messages.capture(history, "CA9", None)) is None


def test_build_summary_formats_fields():
    msg = Message(
        caller_name="Jane Doe",
        callback_number="+15035550134",
        reason="Saturday pickup",
        urgency="high",
        from_number="+15035550134",
        transcript="Caller: hi\nBot: who's calling?",
        call_sid="CA9",
        received_at=datetime.now(timezone.utc),
    )
    body = notify.build_summary(msg)
    assert "Jane Doe" in body
    assert "Reason: Saturday pickup" in body
    assert "Urgency: high" in body
    assert "Callback: +15035550134" in body
    assert "Caller: hi" in body  # transcript included
