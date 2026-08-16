import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import llm, main, messages, notify, relay
from app.config import settings
from app.main import app
from app.messages import Extracted, Message

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_voice_bot_answers_immediately():
    """Default mode: the carrier already rang the cell, so pick up now."""
    r = client.post("/voice")
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text
    assert "<Dial" not in r.text


def test_voice_dial_first_mode_dials_cell(monkeypatch):
    monkeypatch.setattr(settings, "answer_mode", "dial-first")
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
    # Load-bearing: without the subscription Twilio never sends the speaker
    # events, and the silence clock is back to guessing.
    assert 'events="speaker-events"' in r.text


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
    monkeypatch.setattr(settings, "end_grace_seconds", 0.0)

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
    # And the bot signals Twilio to end the call. The type must be exactly
    # "end" — Twilio drops unknown types silently and the call would hang.
    assert end["type"] == "end"
    assert json.loads(end["handoffData"])["reasonCode"] == "message-captured"


def test_ws_strips_marker_with_trailing_whitespace(monkeypatch):
    """A newline after the marker must not leak marker text into TTS."""
    _stub_capture_and_notify(monkeypatch)
    monkeypatch.setattr(settings, "end_grace_seconds", 0.0)

    async def fake_stream(system, messages_):
        for token in ["Take care!", "[[END]]", "\n"]:
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

    assert "".join(spoken) == "Take care!"
    assert "END" not in "".join(spoken)
    assert end["type"] == "end"


def _speaking(name, on):
    """A Twilio speaker event. See docs/conversationrelay-events.md."""
    return {"type": "info", "name": name, "value": "on" if on else "off"}


def _fast_silence(monkeypatch):
    """Shrink the silence timers so tests don't sit through real seconds.

    The watchdog poll shrinks too, since it bounds how long after an
    agentSpeaking=off the loop takes to notice the call went idle.
    """
    monkeypatch.setattr(settings, "silence_prompt_seconds", 0.05)
    monkeypatch.setattr(settings, "silence_hangup_seconds", 0.05)
    monkeypatch.setattr(settings, "end_grace_seconds", 0.0)
    monkeypatch.setattr(relay, "SPEAKING_POLL_SECONDS", 0.02)
    # These tests mostly send no speaker events, which would leave the fallback
    # estimate holding the call non-idle for the real length of every line.
    # Tests that want the estimate set the rate back themselves.
    monkeypatch.setattr(relay, "SPEECH_CHARS_PER_SEC", 10_000.0)


def test_silence_nudges_then_says_goodbye_and_ends(monkeypatch):
    """A caller who never speaks gets one nudge, then a spoken goodbye."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})

        nudge = ws.receive_json()
        assert nudge["type"] == "text"
        assert nudge["token"] == relay.STILL_THERE

        goodbye = ws.receive_json()
        assert goodbye["type"] == "text"
        assert "Goodbye" in goodbye["token"]
        assert goodbye["last"] is True

        end = ws.receive_json()
        assert end["type"] == "end"
        assert json.loads(end["handoffData"])["reasonCode"] == "caller-silent"


def test_speaking_resets_the_silence_clock(monkeypatch):
    """Talking after the nudge earns a fresh nudge, not an immediate hangup."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)

    async def fake_stream(system, messages_):
        yield "Sure thing."

    monkeypatch.setattr(llm, "stream_reply", fake_stream)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        assert ws.receive_json()["token"] == relay.STILL_THERE

        ws.send_json({"type": "prompt", "voicePrompt": "still here"})
        while not ws.receive_json()["last"]:  # drain the reply
            pass

        # nudged was cleared, so the next quiet stretch nudges again.
        assert ws.receive_json()["token"] == relay.STILL_THERE


def test_agent_speech_holds_off_the_nudge(monkeypatch):
    """The bug this all exists for: don't talk over the tail of our own reply.

    Generating a reply takes ~2s; speaking it takes up to ~40s. Only the
    agentSpeaking=off event marks the end, so the clock must not start until
    it arrives — however long the audio runs.
    """
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    # A nudge is due 0.05s after idle, so 0.4s of "audio" is ~8 allowances.
    playing = 0.4

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json(_speaking(relay.AGENT_SPEAKING, True))
        started = time.monotonic()
        time.sleep(playing)
        ws.send_json(_speaking(relay.AGENT_SPEAKING, False))

        assert ws.receive_json()["token"] == relay.STILL_THERE
        assert time.monotonic() - started >= playing


def test_caller_speech_holds_off_the_nudge(monkeypatch):
    """A slow talker is not a silent one. clientSpeaking suppresses the timer
    even though no finalized prompt has arrived yet."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    talking = 0.4

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json(_speaking(relay.CLIENT_SPEAKING, True))
        started = time.monotonic()
        time.sleep(talking)
        ws.send_json(_speaking(relay.CLIENT_SPEAKING, False))

        assert ws.receive_json()["token"] == relay.STILL_THERE
        assert time.monotonic() - started >= talking


def test_end_waits_for_the_goodbye_to_finish_playing(monkeypatch):
    """`end` follows the agent-stop event, not a fixed sleep — so it lands
    when the audio actually ends instead of after a guess."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    # Long enough that a fallback-driven hangup would be unmistakable.
    monkeypatch.setattr(settings, "end_grace_seconds", 30.0)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        assert ws.receive_json()["token"] == relay.STILL_THERE
        assert "Goodbye" in ws.receive_json()["token"]

        # The goodbye is playing. Nothing may be sent until it's done.
        ws.send_json(_speaking(relay.AGENT_SPEAKING, True))
        time.sleep(0.2)
        ws.send_json(_speaking(relay.AGENT_SPEAKING, False))
        started = time.monotonic()

        end = ws.receive_json()

    assert end["type"] == "end"
    assert json.loads(end["handoffData"])["reasonCode"] == "caller-silent"
    assert time.monotonic() - started < 5.0  # not the 30s fallback


def test_unreadable_speaker_event_is_ignored(monkeypatch):
    """The shapes aren't documented by Twilio. A value we can't read must not
    be coerced into a state change — silence detection still has to work."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json({"type": "info", "name": "agentSpeaking", "value": "???"})
        ws.send_json({"type": "info", "name": "tokensPlayed", "value": "Hi there"})

        assert ws.receive_json()["token"] == relay.STILL_THERE


def test_stale_agent_speaking_flag_expires(monkeypatch):
    """A dropped agentSpeaking=off must not hold the call open forever."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    monkeypatch.setattr(relay, "MAX_AGENT_SPEECH_SECONDS", 0.1)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json(_speaking(relay.AGENT_SPEAKING, True))  # no matching "off"

        assert ws.receive_json()["token"] == relay.STILL_THERE


def test_playback_is_estimated_when_no_speaker_events_arrive(monkeypatch):
    """If Twilio never reports playback we fall back to estimating it, so the
    nudge still can't land on top of the bot's own question."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    # 50 chars/sec: the ~20-char nudge is ~0.4s of "audio", against a 0.05s
    # allowance. Without the estimate the goodbye would follow near-instantly.
    monkeypatch.setattr(relay, "SPEECH_CHARS_PER_SEC", 50.0)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        assert ws.receive_json()["token"] == relay.STILL_THERE
        after_nudge = time.monotonic()
        assert "Goodbye" in ws.receive_json()["token"]

    spoken = relay.speech_seconds(relay.STILL_THERE)
    assert time.monotonic() - after_nudge >= spoken * 0.8


def test_caller_gets_full_thinking_time_after_the_bot_stops(monkeypatch):
    """The allowance is measured from when the bot stops talking, not from
    before it started.

    A call can fall idle because a message said so, or because the clock ran
    out on estimated playback. Only the first settles on its own, so the second
    used to fire the nudge the instant the audio ended — the caller got no room
    to answer at all. Both paths owe the same pause.
    """
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    monkeypatch.setattr(settings, "silence_prompt_seconds", 0.5)
    monkeypatch.setattr(relay, "SPEECH_CHARS_PER_SEC", 200.0)
    monkeypatch.setattr(relay, "SPEAKING_POLL_SECONDS", 0.05)

    with client.websocket_connect("/ws") as ws:
        started = time.monotonic()
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        assert ws.receive_json()["token"] == relay.STILL_THERE
        waited = time.monotonic() - started

    playback = relay.speech_seconds(main.GREETING)
    thinking = waited - playback
    assert thinking >= 0.45, f"only {thinking:.2f}s to answer, expected ~0.5s"


def test_speaker_events_switch_the_estimate_off(monkeypatch):
    """A real agent event means Twilio is reporting playback, so the guess is
    dropped — otherwise a slow estimate would hold the call past the truth."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    # Slow enough that, if the estimate were still consulted, the nudge would
    # be held back for many seconds after the agent-stop event says otherwise.
    monkeypatch.setattr(relay, "SPEECH_CHARS_PER_SEC", 2.0)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json(_speaking(relay.AGENT_SPEAKING, True))
        ws.send_json(_speaking(relay.AGENT_SPEAKING, False))
        started = time.monotonic()

        assert ws.receive_json()["token"] == relay.STILL_THERE

    assert time.monotonic() - started < 3.0  # the events won, not the estimate


def test_barge_in_cancels_the_reply_and_the_call_continues(monkeypatch):
    """An interrupt ends agent speech even if the agent-stop event is lost, so
    the clock restarts and the next thing the caller says still gets a turn."""
    _stub_capture_and_notify(monkeypatch)
    _fast_silence(monkeypatch)
    released = asyncio.Event()

    async def fake_stream(system, messages_):
        yield "Let me tell you "
        await released.wait()  # never — the turn is cancelled first
        yield "the rest."

    monkeypatch.setattr(llm, "stream_reply", fake_stream)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json({"type": "prompt", "voicePrompt": "tell me"})
        ws.send_json(_speaking(relay.AGENT_SPEAKING, True))
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "Let me"})

        # Whatever of the reply had already gone out stops there — the rest of
        # the stream is never spoken.
        spoken = []
        while True:
            token = ws.receive_json()["token"]
            # No agentSpeaking=off is sent: the interrupt alone has to clear
            # it, or the call sits non-idle forever and never nudges again.
            if token == relay.STILL_THERE:
                break
            spoken.append(token)

    assert "the rest." not in "".join(spoken)


def test_caller_hangup_still_sends_the_sms(monkeypatch):
    """The caller drops mid-call: finalize still runs off the disconnect."""
    sent = _stub_capture_and_notify(monkeypatch)

    async def fake_stream(system, messages_):
        yield "Got it."

    monkeypatch.setattr(llm, "stream_reply", fake_stream)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "setup", "callSid": "CA123", "customParameters": {}})
        ws.send_json({"type": "prompt", "voicePrompt": "Jane, about Saturday"})
        while not ws.receive_json()["last"]:
            pass
        ws.close()  # hang up without a goodbye

    assert len(sent) == 1
    assert "Missed call" in sent[0]  # capture stub returns no message


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
