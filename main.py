# Setup:
#   pip install -r requirements.txt
#   uvicorn main:app --reload --port 8000
#   Then open http://localhost:8000

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voiceai")

load_dotenv(Path(__file__).parent / ".env")

MINIMAX_API_KEY  = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")

MINIMAX_CHAT_URL  = "https://api.minimax.io/v1/text/chatcompletion_v2"
MINIMAX_TTS_URL   = "https://api.minimax.io/v1/t2a_v2"
MINIMAX_LLM_MODEL = "MiniMax-Text-01"

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly and engaging conversational AI. "
    "Keep responses natural and concise — 2-4 sentences max. "
    "If the user has been talking for a long time without pause, gently redirect them."
)


app = FastAPI()
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


# ── Sentence splitter ─────────────────────────────────────────────────────────

def pop_sentence(buf: str) -> tuple[str, str]:
    """Return (sentence, remainder). sentence='' if no complete sentence yet."""
    m = re.search(r'(?<=[.!?])\s+', buf)
    if m:
        return buf[:m.start() + 1].strip(), buf[m.end():]
    # Also split long clauses so first audio doesn't take too long
    if len(buf) > 120:
        m = re.search(r'(?<=[,;:])\s+', buf)
        if m:
            return buf[:m.start() + 1].strip(), buf[m.end():]
    return '', buf


# ── Session ───────────────────────────────────────────────────────────────────

class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.conversation_history: list[dict] = []
        self.system_prompt: str = DEFAULT_SYSTEM_PROMPT
        self.voice_id: str = "Friendly_Person"
        self.tts_speed: float = 1.0
        self._turn_id: int = 0

    def new_turn(self) -> int:
        self._turn_id += 1
        return self._turn_id

    async def send_json(self, data: dict):
        try:
            await self.ws.send_json(data)
        except Exception:
            pass

    async def send_bytes(self, data: bytes):
        try:
            await self.ws.send_bytes(data)
        except Exception:
            pass

    async def status(self, msg: str):
        await self.send_json({"type": "status", "message": msg})


# ── TTS for a single sentence ─────────────────────────────────────────────────

async def tts_sentence(session: Session, text: str, turn_id: int):
    """Stream one sentence through MiniMax TTS → binary WS frames."""
    if not text.strip() or session._turn_id != turn_id:
        return

    headers = {"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "speech-02-turbo",
        "text": text,
        "stream": True,
        "voice_setting": {
            "voice_id": session.voice_id,
            "speed": session.tts_speed,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
        },
    }

    await session.send_json({"type": "audio_start"})
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", MINIMAX_TTS_URL,
                headers=headers, params={"GroupId": MINIMAX_GROUP_ID}, json=body
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if session._turn_id != turn_id:
                        break
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                        audio_hex = chunk.get("data", {}).get("audio", "") or ""
                        if audio_hex:
                            await session.send_bytes(bytes.fromhex(audio_hex))
                    except (json.JSONDecodeError, ValueError):
                        continue
    except Exception as e:
        if session._turn_id == turn_id:
            log.warning(f"TTS error: {e}")
    finally:
        await session.send_json({"type": "audio_end"})


# ── Main turn handler ─────────────────────────────────────────────────────────

async def handle_turn(session: Session, text: str, turn_id: int):
    if session._turn_id != turn_id:
        return

    # Fix dangling user message if previous turn was interrupted before AI replied
    if session.conversation_history and session.conversation_history[-1]["role"] == "user":
        session.conversation_history[-1]["content"] = text
    else:
        session.conversation_history.append({"role": "user", "content": text})

    await session.status("Thinking...")

    # ── Concurrent LLM generation + per-sentence TTS ──────────────────────────
    # LLM task puts sentences into a queue as they complete.
    # TTS task drains the queue, calling tts_sentence() for each.
    # Both run concurrently so TTS for sentence N overlaps LLM generating sentence N+1.

    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    full_response = ""

    async def llm_task():
        nonlocal full_response
        headers = {
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": MINIMAX_LLM_MODEL,
            "stream": True,
            "messages": [
                {"role": "system", "content": session.system_prompt},
                *session.conversation_history,
            ],
        }
        buf = ""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST", MINIMAX_CHAT_URL,
                    headers=headers, params={"GroupId": MINIMAX_GROUP_ID}, json=body
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if session._turn_id != turn_id:
                            break
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(raw)
                            token = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                            if not token:
                                continue
                            full_response += token
                            buf += token
                            # Stream partial text to frontend
                            await session.send_json({"type": "llm_chunk", "text": full_response})
                            # Enqueue complete sentences immediately
                            while True:
                                sentence, buf = pop_sentence(buf)
                                if not sentence:
                                    break
                                await sentence_queue.put(sentence)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except Exception as e:
            if session._turn_id == turn_id:
                await session.status(f"Error: LLM failed — {e}")
        finally:
            # Flush remaining buffer
            if buf.strip():
                await sentence_queue.put(buf.strip())
            await sentence_queue.put(None)  # sentinel

    async def tts_task():
        first = True
        while True:
            sentence = await sentence_queue.get()
            if sentence is None:
                break
            if session._turn_id != turn_id:
                continue
            if first:
                first = False
                await session.status("Speaking...")
            await tts_sentence(session, sentence, turn_id)

    # Run both concurrently
    await asyncio.gather(llm_task(), tts_task())

    if not full_response.strip():
        return

    if session._turn_id == turn_id:
        # Finalize the full text in the chat bubble
        await session.send_json({"type": "llm_response", "text": full_response})
        session.conversation_history.append({"role": "assistant", "content": full_response})
        await session.send_json({"type": "speaking_done"})


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(ws)
    try:
        while True:
            message = await ws.receive()
            if not message.get("text"):
                continue
            try:
                data = json.loads(message["text"])
            except json.JSONDecodeError:
                continue

            t = data.get("type")

            if t == "user_message":
                text = data.get("text", "").strip()
                if text:
                    turn_id = session.new_turn()
                    asyncio.create_task(handle_turn(session, text, turn_id))

            elif t == "set_prompt":
                p = data.get("prompt", "").strip()
                if p:
                    session.system_prompt = p

            elif t == "set_voice":
                session.voice_id = data.get("voice_id", "Friendly_Person")

            elif t == "set_speed":
                try:
                    session.tts_speed = max(0.5, min(2.0, float(data.get("speed", 1.0))))
                except (ValueError, TypeError):
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await session.status(f"Error: {e}")
        except Exception:
            pass
