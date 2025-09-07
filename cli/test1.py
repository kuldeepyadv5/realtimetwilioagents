import os
import asyncio
import base64
import json
import audioop
import logging
import time
from typing import Optional, Tuple, Deque
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.rest import Client as TwilioClient

# === OpenAI Agentic SDK (your existing imports) ===
from news_agent import get_starting_agent
from agents.realtime import RealtimeRunner, RealtimeSession, RealtimeSessionEvent

# -----------------------
# Env & basic config
# -----------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

# Optional, only needed for outbound calling
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_CALLER_ID", "")

# Public base URL of THIS server (ngrok/caddy/https). Used in TwiML + stream target.
# Example: https://abcd-1234.ngrok-free.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_HOST", "").rstrip("/")
if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL is not set (e.g., https://<your-domain>)")

# Audio settings
TWILIO_SR = 8000        # Twilio Media Streams: 8 kHz μ-law
MODEL_SR = 24000        # OpenAI Realtime default/high-quality path
FRAME_MS = 20           # Twilio sends/expects 20 ms frames
BYTES_PER_SAMPLE = 2    # int16

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("prism.voice")

app = FastAPI()

# -----------------------
# Utilities: resample + μ-law
# -----------------------
class AudioBridge:
    """
    Bridge buffers and rate conversion between Twilio (8k u-law) and model (24k PCM16).
    Uses stdlib audioop for low-latency, dependency-free conversion.
    """
    def __init__(self):
        self._to_model_state = None   # audioop.ratecv state 8k->24k
        self._to_caller_state = None  # audioop.ratecv state 24k->8k

    def twilio_ulaw_to_model_pcm16(self, b_ulaw: bytes) -> bytes:
        # μ-law -> linear16 at 8k
        lin16_8k = audioop.ulaw2lin(b_ulaw, BYTES_PER_SAMPLE)
        # 8k -> 24k (ratecv keeps state for streaming quality)
        lin16_24k, self._to_model_state = audioop.ratecv(
            lin16_8k, BYTES_PER_SAMPLE, 1, TWILIO_SR, MODEL_SR, self._to_model_state
        )
        return lin16_24k

    def model_pcm16_to_twilio_ulaw_chunks(self, pcm16_24k: bytes) -> Deque[str]:
        """
        Convert model PCM16@24k to μ-law@8k 20ms frames and return base64 strings ready for Twilio.
        """
        # 24k -> 8k
        lin16_8k, self._to_caller_state = audioop.ratecv(
            pcm16_24k, BYTES_PER_SAMPLE, 1, MODEL_SR, TWILIO_SR, self._to_caller_state
        )
        # Split into 20ms linear16 frames at 8k: 160 samples * 2 bytes = 320 bytes
        frame_bytes = int(TWILIO_SR * FRAME_MS / 1000) * BYTES_PER_SAMPLE  # 320
        out = deque()
        for i in range(0, len(lin16_8k), frame_bytes):
            chunk = lin16_8k[i:i+frame_bytes]
            if len(chunk) < frame_bytes:
                break
            ulaw = audioop.lin2ulaw(chunk, BYTES_PER_SAMPLE)  # 160 bytes
            out.append(base64.b64encode(ulaw).decode("ascii"))
        return out


# -----------------------
# TwiML: connect call -> stream
# -----------------------
@app.post("/call/voice")
def call_voice():
    """
    Inbound voice webhook (set this in Twilio number -> Voice -> A Call Comes In).
    Returns TwiML that connects live audio to our /media WebSocket as a Twilio Media Stream.
    """
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{PUBLIC_BASE_URL.replace('https://', 'wss://')}/media">
      <Parameter name="fromServer" value="prism-bridge"/>
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# -----------------------
# (Optional) Outbound dialer
# -----------------------
@app.post("/call/start")
async def start_outbound_call(request: Request):
    """
    Body JSON: { "to": "+9198xxxxxx" }
    Requires TWILIO_* env vars.
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return {"ok": False, "error": "Twilio creds or FROM not set"}

    to ="+919671116213"
    if not to:
        return {"ok": False, "error": "missing 'to' number"}

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    # Twilio hits /call/voice to get TwiML which streams to /media
    call = client.calls.create(
        to=to,
        from_=TWILIO_FROM_NUMBER,
        url=f"{PUBLIC_BASE_URL}/call/voice"
    )
    log.info(f"Started outbound call SID={call.sid}")
    return {"ok": True, "sid": call.sid}


# -----------------------
# Media Stream <-> Realtime bridge
# -----------------------
@app.websocket("/media")
async def media_ws(ws: WebSocket):
    await ws.accept()
    log.info("Twilio WS connected")

    bridge = AudioBridge()

    # Runtime flags
    stream_sid: Optional[str] = None
    is_playing_tts: bool = False

    # Queue of base64 ulaw frames to send to caller at 20ms cadence
    to_caller: Deque[str] = deque()
    to_caller_lock = asyncio.Lock()

    # --- Start OpenAI Realtime session
    agent = get_starting_agent()
    runner = RealtimeRunner(agent)

    async with await runner.run(
        model_config={
            "api_key": OPENAI_API_KEY,
            "initial_model_settings": {
                "voice": "alloy",
                "output_audio_format": "pcm16"  # we expect PCM16 24k from model
            }
        }
    ) as session: 
        async def pump_model_events():
            nonlocal is_playing_tts
            try:
                async for event in session:
                    et = event.type

                    if et == "agent_start":
                        is_playing_tts = True

                    elif et == "audio":
                        # Model produced PCM16@24k bytes -> μ-law@8k 20ms frames
                        pcm16_24k: bytes = event.audio.data
                        chunks = bridge.model_pcm16_to_twilio_ulaw_chunks(pcm16_24k)
                        async with to_caller_lock:
                            to_caller.extend(chunks)

                    elif et == "audio_end":
                        is_playing_tts = False

                    elif et == "audio_interrupted":
                        is_playing_tts = False
                        # Flush pending audio to caller
                        async with to_caller_lock:
                            to_caller.clear()

                    elif et == "error":
                        log.error(f"Model error: {event.error}")
            except Exception as e:
                log.exception(f"pump_model_events crashed: {e}")

        async def flush_to_caller():
            """
            Send media frames to Twilio every 20ms if available.
            Twilio expects messages:
            {"event":"media","streamSid": "...","media":{"payload": "<base64 ulaw>"}}
            """
            try:
                while True:
                    frame = None
                    async with to_caller_lock:
                        if to_caller:
                            frame = to_caller.popleft()
                    if frame and stream_sid:
                        msg = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": frame},
                        }
                        await ws.send_text(json.dumps(msg))
                    await asyncio.sleep(FRAME_MS / 1000.0)
            except Exception as e:
                log.exception(f"flush_to_caller crashed: {e}")

        async def read_from_twilio():
            """
            Receive Twilio WS events:
              - "start": capture streamSid
              - "media": base64 μ-law audio from caller -> model.send_audio(PCM16@24k)
              - "mark"/"stop": handle if needed
            Implements barge-in: if caller speaks while is_playing_tts, cancel current model response.
            """
            nonlocal stream_sid, is_playing_tts
            # Simple VAD threshold on amplitude to trigger barge-in decisions
            VAD_PEAK = 2000  # tweak if needed

            try:
                while True:
                    raw = await ws.receive_text()
                    evt = json.loads(raw)
                    et = evt.get("event")

                    if et == "start":
                        stream_sid = evt["start"]["streamSid"]
                        log.info(f"Stream started: {stream_sid}")

                    elif et == "media":
                        payload_b64 = evt["media"]["payload"]
                        ulaw_bytes = base64.b64decode(payload_b64)
                        # μ-law -> 24k PCM16
                        pcm16_24k = bridge.twilio_ulaw_to_model_pcm16(ulaw_bytes)

                        # Barge-in: detect non-trivial speech energy while TTS is playing
                        if is_playing_tts:
                            # rough amplitude check
                            try:
                                peak = audioop.max(pcm16_24k, BYTES_PER_SAMPLE)
                            except Exception:
                                peak = 0
                            if peak and peak > VAD_PEAK:
                                # cancel current model response and flush audio to caller
                                is_playing_tts = False
                                await session.send_event({"type": "response.cancel"})
                                async with to_caller_lock:
                                    to_caller.clear()

                        await session.send_audio(pcm16_24k)

                    elif et == "mark":
                        # optional: custom markers
                        pass

                    elif et == "stop":
                        log.info("Twilio stream stop")
                        break

            except WebSocketDisconnect:
                log.info("Twilio WS disconnected")
            except Exception as e:
                log.exception(f"read_from_twilio crashed: {e}")

        # fire tasks
        tasks = [
            asyncio.create_task(pump_model_events()),
            asyncio.create_task(flush_to_caller()),
            asyncio.create_task(read_from_twilio()),
        ]

        # wait for any task to finish (disconnect, error, or stop)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception():
                log.exception("Task error", exc_info=t.exception())

    # ensure Twilio side closed
    try:
        await ws.close()
    except Exception:
        pass
    log.info("Session ended")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
