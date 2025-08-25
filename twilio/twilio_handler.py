from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket

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
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


@function_tool
def get_current_time() -> str:
    """Get the current time."""
    return f"The current time is {datetime.now().strftime('%H:%M:%S')}"


agent = RealtimeAgent(
    name="Twilio Assistant",
    instructions="You are a helpful voice assistant for phone conversations. Always start with 'Hi Kuldeep, how are you?' when the conversation begins. Listen carefully to the user's questions and provide clear, concise responses. Ask clarifying questions if needed, but keep the conversation flowing naturally. Be conversational and engaging while staying focused on helping the user.",
    tools=[get_weather, get_current_time],
)


class TwilioHandler:
    def __init__(self, twilio_websocket: WebSocket):
        self.twilio_websocket = twilio_websocket
        self._message_loop_task: asyncio.Task[None] | None = None
        self.session: RealtimeSession | None = None
        self.playback_tracker = RealtimePlaybackTracker()

        # Audio buffering configuration (matching CLI demo)
        self.CHUNK_LENGTH_S = 0.05  # 50ms chunks like CLI demo
        self.SAMPLE_RATE = 8000  # Twilio uses 8kHz for g711_ulaw
        self.BUFFER_SIZE_BYTES = int(self.SAMPLE_RATE * self.CHUNK_LENGTH_S)  # 50ms worth of audio

        self._stream_sid: str | None = None
        self._audio_buffer: bytearray = bytearray()
        self._last_buffer_send_time = time.time()

        # Mark event tracking for playback
        self._mark_counter = 0
        self._mark_data: dict[
            str, tuple[str, int, int]
        ] = {}  # mark_id -> (item_id, content_index, byte_count)

    async def start(self) -> None:
        """Start the session."""
        runner = RealtimeRunner(agent)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        print(f"Using OpenAI API key: {api_key[:20]}..." if api_key else "No API key found")
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
                    "instructions": "You are a helpful voice assistant for phone conversations. Always start with 'Hi Kuldeep, how are you?' when the conversation begins. Listen carefully to the user's questions and provide clear, concise responses. Ask clarifying questions if needed, but keep the conversation flowing naturally. Be conversational and engaging while staying focused on helping the user.",
                },
                "playback_tracker": self.playback_tracker,
            }
        )

        await self.session.enter()
        print("Realtime session entered successfully")

        # Check if WebSocket is already accepted (for outbound calls)
        try:
            await self.twilio_websocket.accept()
            print("Twilio WebSocket connection accepted")
        except Exception as e:
            print(f"WebSocket already accepted or error: {e}")

        # Start all background tasks
        self._realtime_session_task = asyncio.create_task(self._realtime_session_loop())
        self._message_loop_task = asyncio.create_task(self._twilio_message_loop())
        self._buffer_flush_task = asyncio.create_task(self._buffer_flush_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        print("All background tasks started")

    async def wait_until_done(self) -> None:
        """Wait until the session is done."""
        assert self._message_loop_task is not None
        try:
            # Wait for the main message loop to complete
            await self._message_loop_task
        except Exception as e:
            print(f"Error in wait_until_done: {e}")
        finally:
            # Cleanup all tasks
            await self._cleanup_tasks()

    async def _cleanup_tasks(self) -> None:
        """Clean up all background tasks."""
        tasks_to_cancel = [
            self._realtime_session_task,
            self._message_loop_task,
            self._buffer_flush_task,
            self._keepalive_task
        ]

        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"Error cancelling task: {e}")

        # Close the session if it exists
        if self.session:
            try:
                await self.session.close()
                print("Session closed successfully")
            except Exception as e:
                print(f"Error closing session: {e}")

    async def _keepalive_loop(self) -> None:
        """Send periodic keepalive messages to maintain WebSocket connection."""
        try:
            while True:
                await asyncio.sleep(20)  # Send keepalive every 20 seconds
                try:
                    # Send a simple keepalive message
                    await self.twilio_websocket.send_text(json.dumps({"event": "keepalive"}))
                    print("Keepalive sent")
                except Exception as e:
                    print(f"Error sending keepalive: {e}")
                    break
        except asyncio.CancelledError:
            print("Keepalive loop cancelled")
        except Exception as e:
            print(f"Error in keepalive loop: {e}")

    async def _realtime_session_loop(self) -> None:
        """Listen for events from the realtime session."""
        assert self.session is not None
        try:
            async for event in self.session:
                try:
                    await self._handle_realtime_event(event)
                except Exception as event_error:
                    print(f"Error handling realtime event {event.type}: {event_error}")
                    continue
        except Exception as e:
            print(f"Error in realtime session loop: {e}")
            # Try to cleanup gracefully
            if self.session:
                try:
                    await self.session.close()
                except Exception as close_error:
                    print(f"Error closing session: {close_error}")

    async def _twilio_message_loop(self) -> None:
        """Listen for messages from Twilio WebSocket and handle them."""
        try:
            while True:
                try:
                    message_text = await self.twilio_websocket.receive_text()
                    if not message_text:
                        continue
                    message = json.loads(message_text)
                    await self._handle_twilio_message(message)
                except json.JSONDecodeError as e:
                    print(f"Failed to parse Twilio message as JSON: {e}")
                    continue
                except Exception as msg_error:
                    print(f"Error processing Twilio message: {msg_error}")
                    # Continue processing other messages
                    continue
        except Exception as e:
            print(f"Error in Twilio message loop: {e}")
            # Don't re-raise, let the task end gracefully

    async def _handle_realtime_event(self, event: RealtimeSessionEvent) -> None:
        """Handle events from the realtime session."""
        try:
            if event.type == "audio":
                base64_audio = base64.b64encode(event.audio.data).decode("utf-8")
                await self.twilio_websocket.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {"payload": base64_audio},
                        }
                    )
                )

                # Send mark event for playback tracking
                self._mark_counter += 1
                mark_id = str(self._mark_counter)
                self._mark_data[mark_id] = (
                    event.audio.item_id,
                    event.audio.content_index,
                    len(event.audio.data),
                )

                await self.twilio_websocket.send_text(
                    json.dumps(
                        {
                            "event": "mark",
                            "streamSid": self._stream_sid,
                            "mark": {"name": mark_id},
                        }
                    )
                )

            elif event.type == "audio_interrupted":
                print("Audio interrupted - clearing audio stream")
                try:
                    await self.twilio_websocket.send_text(
                        json.dumps({"event": "clear", "streamSid": self._stream_sid})
                    )
                except Exception as e:
                    print(f"Error sending clear event: {e}")
            elif event.type == "audio_end":
                print("Audio playback ended")
            elif event.type == "raw_model_event":
                # Handle model events for conversation flow
                pass
            elif event.type == "response":
                print(f"AI response started: {event.response}")
            elif event.type == "response_end":
                print("AI response completed")
            else:
                print(f"Unhandled event type: {event.type}")
        except Exception as e:
            print(f"Error handling realtime event: {e}")

    async def _handle_twilio_message(self, message: dict[str, Any]) -> None:
        """Handle incoming messages from Twilio Media Stream."""
        try:
            event = message.get("event")

            if event == "connected":
                print("Twilio media stream connected")
            elif event == "start":
                start_data = message.get("start", {})
                stream_sid = start_data.get("streamSid")
                if not self._stream_sid:
                    self._stream_sid = stream_sid
                    print(f"Media stream started with SID: {self._stream_sid}")
                    # Send a small amount of silence to initialize the audio stream
                    await asyncio.sleep(0.1)
                    # Notify that media stream is ready for conversation
                    print("Media stream ready for conversation")
                else:
                    print(f"Media stream start event received again for SID: {stream_sid} (already initialized)")
            elif event == "media":
                await self._handle_media_event(message)
            elif event == "mark":
                await self._handle_mark_event(message)
            elif event == "stop":
                print("Media stream stopped")
            elif event == "keepalive":
                print("Received keepalive from Twilio")
        except Exception as e:
            print(f"Error handling Twilio message: {e}")

    async def _handle_media_event(self, message: dict[str, Any]) -> None:
        """Handle audio data from Twilio - buffer it before sending to OpenAI."""
        media = message.get("media", {})
        payload = media.get("payload", "")

        if payload:
            try:
                # Decode base64 audio from Twilio (µ-law format)
                ulaw_bytes = base64.b64decode(payload)

                # Add original µ-law to buffer for OpenAI (they expect µ-law)
                self._audio_buffer.extend(ulaw_bytes)

                # Send buffered audio if we have enough data
                if len(self._audio_buffer) >= self.BUFFER_SIZE_BYTES:
                    await self._flush_audio_buffer()

            except Exception as e:
                print(f"Error processing audio from Twilio: {e}")

    async def _handle_mark_event(self, message: dict[str, Any]) -> None:
        """Handle mark events from Twilio to update playback tracker."""
        try:
            mark_data = message.get("mark", {})
            mark_id = mark_data.get("name", "")

            # Look up stored data for this mark ID
            if mark_id in self._mark_data:
                item_id, item_content_index, byte_count = self._mark_data[mark_id]

                # Convert byte count back to bytes for playback tracker
                audio_bytes = b"\x00" * byte_count  # Placeholder bytes

                # Update playback tracker
                self.playback_tracker.on_play_bytes(item_id, item_content_index, audio_bytes)
                print(
                    f"Playback tracker updated: {item_id}, index {item_content_index}, {byte_count} bytes"
                )

                # Clean up the stored data
                del self._mark_data[mark_id]

        except Exception as e:
            print(f"Error handling mark event: {e}")

    async def _flush_audio_buffer(self) -> None:
        """Send buffered audio to OpenAI."""
        if not self._audio_buffer or not self.session:
            return

        try:
            # Send the buffered audio
            buffer_data = bytes(self._audio_buffer)
            await self.session.send_audio(buffer_data)

            # Clear the buffer
            self._audio_buffer.clear()
            self._last_buffer_send_time = time.time()

        except Exception as e:
            print(f"Error sending buffered audio to OpenAI: {e}")

    async def _buffer_flush_loop(self) -> None:
        """Periodically flush audio buffer to prevent stale data."""
        try:
            while True:
                await asyncio.sleep(self.CHUNK_LENGTH_S)  # Check every 50ms

                # If buffer has data and it's been too long since last send, flush it
                current_time = time.time()
                if (
                    self._audio_buffer
                    and current_time - self._last_buffer_send_time > self.CHUNK_LENGTH_S * 2
                ):
                    await self._flush_audio_buffer()

        except Exception as e:
            print(f"Error in buffer flush loop: {e}")
