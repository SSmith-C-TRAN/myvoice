"""Local ConversationRelay simulator — talk to the bot by typing.

Mimics what Twilio sends over the /ws WebSocket so you can exercise the full
LLM turn loop without a phone call or a tunnel. Needs ANTHROPIC_API_KEY set and
the server running (uvicorn app.main:app --port 8080).

    pip install websockets
    python scripts/simulate_relay.py

Type a message and press enter to "speak" as the caller. Ctrl-C to hang up.
"""

import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8080/ws"


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
        print("connected. type a message as the caller (Ctrl-C to hang up).\n")

        loop = asyncio.get_event_loop()
        while True:
            text = await loop.run_in_executor(None, sys.stdin.readline)
            if not text:
                break
            text = text.strip()
            if not text:
                continue

            await ws.send(json.dumps({"type": "prompt", "voicePrompt": text}))

            # Collect streamed tokens until the end-of-turn marker.
            print("bot: ", end="", flush=True)
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("type") == "text":
                    print(reply.get("token", ""), end="", flush=True)
                    if reply.get("last"):
                        break
            print("\n")

            # The bot may end the call itself (end-session) right after the turn.
            # Stand in for Twilio: close the socket so the server finalizes.
            try:
                nxt = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                if nxt.get("type") == "end-session":
                    print("[bot ended the call — hanging up]")
                    break
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nhung up.")
