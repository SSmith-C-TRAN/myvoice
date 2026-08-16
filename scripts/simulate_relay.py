"""Local ConversationRelay simulator — talk to the bot by typing.

Mimics what Twilio sends over the /ws WebSocket so you can exercise the full
LLM turn loop without a phone call or a tunnel. Needs ANTHROPIC_API_KEY set and
the server running (uvicorn app.main:app --port 8080).

    pip install websockets
    python scripts/simulate_relay.py

Type a message and press enter to "speak" as the caller. Ctrl-C to hang up.

Because silence detection keys off Twilio's speaker events, this stands in for
them too: it reports the "agent" speaking for as long as the reply would
plausibly take to say out loud, so a quiet terminal looks like a quiet caller
rather than like a caller talking over the bot. Typing controls the rest:

    /talking     you started speaking (clientSpeaking on) — holds the clock
    /done        you stopped speaking (clientSpeaking off)
    /barge       barge in over the bot (interrupt)
    /mute        stop faking agent speech, to see the no-events fallback
"""

import asyncio
import json
import sys
import time

import websockets

WS_URL = "ws://localhost:8080/ws"

# Rough speaking rate, used *only* here to decide how long to pretend the
# agent's TTS runs. The server no longer estimates anything.
CHARS_PER_SEC = 14.0
GREETING_SECONDS = 7.0


async def speaker(ws, name: str, on: bool) -> None:
    """Send a speaker event in the shape Twilio uses. See
    docs/conversationrelay-events.md."""
    await ws.send(
        json.dumps({"type": "info", "name": name, "value": "on" if on else "off"})
    )


async def play(ws, text: str, seconds: float, muted: bool) -> None:
    """Pretend Twilio is speaking `text`, and report start and stop like it
    would. This is what keeps the silence clock from running over the bot."""
    if muted:
        return
    await speaker(ws, "agentSpeaking", True)
    await asyncio.sleep(seconds)
    await speaker(ws, "agentSpeaking", False)
    print(f"[agent finished speaking after {seconds:.1f}s]")


async def main() -> None:
    async with websockets.connect(WS_URL) as ws:
        # 1. setup — the same first message Twilio sends on connect.
        await ws.send(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CAlocaltest",
                    "customParameters": {"from": "+15035550134"},
                }
            )
        )
        print("connected. type a message as the caller (Ctrl-C to hang up).")
        print("commands: /talking /done /barge /mute\n")

        muted = False
        # Twilio plays the welcome greeting before anyone says anything.
        asyncio.create_task(play(ws, "", GREETING_SECONDS, muted))

        loop = asyncio.get_event_loop()
        while True:
            text = await loop.run_in_executor(None, sys.stdin.readline)
            if not text:
                break
            text = text.strip()
            if not text:
                continue

            if text == "/talking":
                await speaker(ws, "clientSpeaking", True)
                print("[you are speaking — the clock is held]")
                continue
            if text == "/done":
                await speaker(ws, "clientSpeaking", False)
                print("[you stopped speaking — the clock restarts]")
                continue
            if text == "/barge":
                await ws.send(json.dumps({"type": "interrupt"}))
                print("[barged in]")
                continue
            if text == "/mute":
                muted = not muted
                state = "off" if muted else "on"
                print(f"[faked speaker events {state}]")
                continue

            await ws.send(json.dumps({"type": "prompt", "voicePrompt": text}))

            # Collect streamed tokens until the end-of-turn marker.
            print("bot: ", end="", flush=True)
            spoken = ""
            started = time.monotonic()
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("type") == "text":
                    token = reply.get("token", "")
                    spoken += token
                    print(token, end="", flush=True)
                    if reply.get("last"):
                        break
            print("\n")

            # Generation is done, but on a real call TTS is only starting. Fake
            # the rest of the playback so the server sees the gap it really has
            # — this is the whole bug the speaker events exist to fix.
            remaining = len(spoken) / CHARS_PER_SEC - (time.monotonic() - started)
            asyncio.create_task(play(ws, spoken, max(0.0, remaining), muted))

            # The bot may end the call itself right after the turn. Stand in
            # for Twilio: close the socket so the server finalizes.
            try:
                nxt = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                if nxt.get("type") == "end":
                    print("[bot ended the call — hanging up]")
                    break
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nhung up.")
