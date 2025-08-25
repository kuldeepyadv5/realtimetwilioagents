"""
Simplified Twilio Handler - Fixed Audio Issues
- Direct audio forwarding (no history processing)
- Removed sys.exit() calls
- Clean WebSocket handling
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from agents import function_tool
from agents.realtime import (
    RealtimeAgent,
    RealtimePlaybackTracker,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."

@function_tool
def get_current_time() -> str:
    return f"The current time is {datetime.now().strftime('%H:%M:%S')}"

agent = RealtimeAgent(
    name="Twilio Assistant",
    instructions=(
        "You are a helpful voice assistant for phone conversations. "
        "Always start with 'Hi Kuldeep, how are you?' when the conversation begins. "
        "Listen carefully to the user's questions and provide clear, concise responses. "
        "Ask clarifying questions if needed, but keep the conversation flowing naturally. "
        "Be conversational and engaging while staying focused on helping the user."
    ),
    tools=[get_weather, get_current_time],
)

class TwilioHandler:
    def __init__(self, twilio_websocket: WebSocket):
        self.twilio_websocket = twilio_websocket
        self.session: RealtimeSession | None = None
        self.playback_tracker = RealtimePlaybackTracker()

        # 50ms chunks @ 8kHz μ-law
        self.CHUNK_LENGTH_S = 0.05
        self.SAMPLE_RATE = 8000
        self.BUFFER_SIZE_BYTES = int(self.SAMPLE_RATE * self.CHUNK_LENGTH_S)  # ≈400 bytes

        self._stream_sid: str | None = None
        self._audio_buffer: bytearray = bytearray()
        self._last_buffer_send_time = time.time()
        self._mark_counter = 0

    def set_stream_sid(self, stream_sid: str | None) -> None:
        self._stream_sid = stream_sid

    async def start(self) -> None:
        # Start the OpenAI Realtime session
        runner = RealtimeRunner(agent)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.session = await runner.run(
            model_config={
                "api_key": api_key,
                "initial_model_settings": {
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "semantic_vad",
                        "interrupt_response": True,
                        "create_response": True,
                    },
                },
                "playback_tracker": self.playback_tracker,
            }
        )
        await self.session.enter()

        # Background tasks
        self._rt_task = asyncio.create_task(self._realtime_session_loop())
        self._msg_task = asyncio.create_task(self._twilio_message_loop())
        self._flush_task = asyncio.create_task(self._buffer_flush_loop())

    async def wait_until_done(self) -> None:
        try:
            await self._msg_task
        finally:
            await self._cleanup()

    async def _cleanup(self):
        for task in [getattr(self, n, None) for n in ("_rt_task", "_msg_task", "_flush_task")]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass

    async def _realtime_session_loop(self) -> None:
        assert self.session is not None
        async for event in self.session:
            try:
                await self._handle_realtime_event(event)
            except Exception as e:
                print(f"Error handling realtime event {event.type}: {e}")

    async def _twilio_message_loop(self) -> None:
        while True:
            try:
                message = json.loads(await self.twilio_websocket.receive_text())
                await self._handle_twilio_message(message)
            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"Error in Twilio message loop: {e}")
                # DO NOT sys.exit(); just break the loop so cleanup runs
                break

    async def _handle_realtime_event(self, event: RealtimeSessionEvent) -> None:
        # Forward only realtime audio frames; do not replay history
        if event.type == "audio":
            if not self._stream_sid:
                return
            b64 = base64.b64encode(event.audio.data).decode("utf-8")
            await self.twilio_websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": b64},
            }))
            # Optional mark so Twilio confirms when played
            self._mark_counter += 1
            await self.twilio_websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": str(self._mark_counter)},
            }))
        elif event.type == "audio_interrupted" and self._stream_sid:
            await self.twilio_websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": self._stream_sid
            }))

    async def _handle_twilio_message(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "connected":
            return
        if event == "start":
            start = message.get("start", {})
            # If server-level endpoint already set streamSid, keep it
            self._stream_sid = self._stream_sid or start.get("streamSid")
            return
        if event == "media":
            await self._handle_media_event(message)
        # mark/dtmf/stop can be ignored or logged

    async def _handle_media_event(self, message: dict[str, Any]) -> None:
        media = message.get("media", {})
        payload = media.get("payload", "")
        if not payload:
            return
        try:
            ulaw_bytes = base64.b64decode(payload)
            self._audio_buffer.extend(ulaw_bytes)
            if len(self._audio_buffer) >= self.BUFFER_SIZE_BYTES:
                await self._flush_audio_buffer()
        except Exception as e:
            print(f"Error processing inbound audio: {e}")

    async def _flush_audio_buffer(self) -> None:
        if not self._audio_buffer or not self.session:
            return
        data = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        self._last_buffer_send_time = time.time()
        try:
            await self.session.send_audio(data)
        except Exception as e:
            print(f"Error sending audio to OpenAI: {e}")

    async def _buffer_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.CHUNK_LENGTH_S)
            if self._audio_buffer and (time.time() - self._last_buffer_send_time) > (self.CHUNK_LENGTH_S * 2):
                await self._flush_audio_buffer()