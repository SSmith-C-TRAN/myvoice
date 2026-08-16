from fastapi.testclient import TestClient

from app import llm
from app.main import app

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
    assert 'timeout="15"' in body
    assert "+18084649192" in body
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


def test_handoff_hangs_up():
    r = client.post("/voice/handoff")
    assert r.status_code == 200
    assert "<Hangup/>" in r.text


def test_ws_streams_llm_reply(monkeypatch):
    async def fake_stream(system, messages):
        assert messages[-1] == {"role": "user", "content": "hi there"}
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

    # Final token is the empty end-of-turn marker.
    assert tokens[-1] == ""
    assert "".join(tokens) == "Hello! Who's calling?"
