from __future__ import annotations

import asyncio
import base64
import json
import os ,sys
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
        self.CHUNK_LENGTH_S = 0.09  # 50ms chunks like CLI demo
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

        # Track processed audio items to avoid duplicates
        self._processed_audio_items: set[str] = set()

    async def start(self) -> None:
        """Start the session."""
        # Clear any previous state
        self._processed_audio_items.clear()

        runner = RealtimeRunner(agent)
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

        # WebSocket should already be accepted by the media stream endpoint
        print("Using existing WebSocket connection for Twilio handler")

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

                    # Debug: Show incoming messages from Twilio
                    event_type = message.get('event', 'unknown')
                    if event_type == 'media':
                        pass
                        # print(f"📥 INCOMING: {event_type} - {len(message.get('media', {}).get('payload', ''))} bytes")
                    else:
                        print(f"📥 INCOMING: {event_type} - {message}")

                    await self._handle_twilio_message(message)
                except json.JSONDecodeError as e:
                    print(f"Failed to parse Twilio message as JSON: {e}")
                    continue
                except Exception as msg_error:
                    print(f"❌ Error processing Twilio message: {msg_error}")
                    sys.exit()
                    # Continue processing other messages
                    continue
        except Exception as e:
            print(f"Error in Twilio message loop: {e}")
            # Don't re-raise, let the task end gracefully

    async def _handle_realtime_event(self, event: RealtimeSessionEvent) -> None:
        """Handle events from the realtime session."""
        try:
            # print(f"\n🎯 EVENT TYPE: {event.type}")
            if event.type == "audio":
                print(f"🎵 DIRECT AUDIO EVENT: Processing {len(event.audio.data)} bytes")
                base64_audio = base64.b64encode(event.audio.data).decode("utf-8")
                print(f"📤 Sending direct audio payload: {len(base64_audio)} chars")

                await self.twilio_websocket.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {"payload": base64_audio},
                        }
                    )
                )
                print("✅ Direct audio sent to Twilio")

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
            elif event.type == "history_updated":
                # Extract and display conversation text
                await self._extract_conversation_text(event)
                # Extract and play AI audio from conversation history
                await self._handle_history_audio(event)
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

    async def _handle_history_audio(self, event) -> None:
        """Extract and send AI audio from conversation history to Twilio."""
        try:
            if not hasattr(event, 'history') or not event.history:
                return

            # Track which item_ids we've already processed to avoid duplicates
            # (Already initialized in constructor)

            # Look for the most recent assistant message with audio
            for item in reversed(event.history):
                if hasattr(item, 'role') and item.role == 'assistant' and hasattr(item, 'content') and hasattr(item, 'item_id'):
                    item_id = item.item_id

                    # Skip if we've already processed this item's audio
                    if item_id in self._processed_audio_items:
                        continue

                    for content_item in item.content:
                        if hasattr(content_item, 'type') and content_item.type == 'audio' and hasattr(content_item, 'audio') and content_item.audio:
                            # Found AI audio - send it to Twilio
                            audio_data = content_item.audio
                            print(f"🎵 Found AI audio data, type: {type(audio_data)}")

                            if isinstance(audio_data, bytes):
                                # Convert bytes to base64 for Twilio
                                base64_audio = base64.b64encode(audio_data).decode("utf-8")
                                print(f"📤 Sending {len(audio_data)} bytes of AI audio to Twilio")
                            elif isinstance(audio_data, str):
                                # Already base64 encoded - decode and re-encode to ensure proper format
                                try:
                                    # Decode base64 to verify it's valid, then re-encode
                                    decoded_bytes = base64.b64decode(audio_data)
                                    base64_audio = base64.b64encode(decoded_bytes).decode("utf-8")
                                    print(f"📤 Re-encoded base64 AI audio to Twilio (original length: {len(audio_data)}, decoded bytes: {len(decoded_bytes)})")
                                except Exception as decode_error:
                                    print(f"❌ Error decoding base64 audio: {decode_error}")
                                    continue
                            else:
                                print(f"❌ Unknown audio data type: {type(audio_data)}")
                                continue

                            try:
                                # Send the audio in smaller chunks if it's very large
                                chunk_size = 4000  # ~0.25 seconds of μ-law audio at 8kHz
                                audio_bytes = base64.b64decode(base64_audio)

                                print(f"🎵 Sending AI audio to Twilio: {len(audio_bytes)} bytes total")

                                if len(audio_bytes) > chunk_size:
                                    # Send in chunks
                                    print(f"📦 Audio is large, sending in {chunk_size} byte chunks")
                                    for i in range(0, len(audio_bytes), chunk_size):
                                        chunk = audio_bytes[i:i + chunk_size]
                                        chunk_base64 = base64.b64encode(chunk).decode("utf-8")

                                        print(f"📤 Sending chunk {i//chunk_size + 1}: {len(chunk)} bytes")
                                        await self.twilio_websocket.send_text(
                                            json.dumps(
                                                {
                                                    "event": "media",
                                                    "streamSid": self._stream_sid,
                                                    "media": {"payload": chunk_base64},
                                                }
                                            )
                                        )
                                        await asyncio.sleep(0.001)  # Very small delay between chunks
                                else:
                                    # Send as single payload
                                    print(f"📤 Sending audio as single payload: {len(audio_bytes)} bytes")
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
                                audio_bytes_len = len(audio_bytes)
                                self._mark_data[mark_id] = (
                                    item_id,
                                    0,  # content_index
                                    audio_bytes_len,  # byte_count
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

                                # Mark this item as processed
                                self._processed_audio_items.add(item_id)

                                print(f"✅ Successfully sent AI audio response to Twilio ({audio_bytes_len} bytes)")
                                return  # Only send the most recent audio

                            except Exception as send_error:
                                print(f"❌ Error sending audio to Twilio: {send_error}")
                                continue

        except Exception as e:
            print(f"❌ Error handling history audio: {e}")
            # Fallback: try to extract text and use Twilio's text-to-speech
            await self._fallback_text_to_speech(event)

    async def _extract_conversation_text(self, event) -> None:
        """Extract and display conversation text from history events."""
        try:
            if not hasattr(event, 'history') or not event.history:
                return

            print("\n📝 CONVERSATION UPDATE:")

            for item in event.history:
                if hasattr(item, 'role') and hasattr(item, 'content'):
                    role = item.role
                    content_list = item.content if hasattr(item, 'content') else []

                    for content_item in content_list:
                        if hasattr(content_item, 'type'):
                            if content_item.type == 'text' and hasattr(content_item, 'text'):
                                # Display text content
                                if role == 'user':
                                    print(f"👤 USER: '{content_item.text}'")
                                elif role == 'assistant':
                                    print(f"🤖 AI: '{content_item.text}'")

                            elif content_item.type == 'input_audio' and hasattr(content_item, 'transcript'):
                                # Display user speech transcription
                                if content_item.transcript:
                                    print(f"👤 USER (speech): '{content_item.transcript}'")

                            elif content_item.type == 'audio' and hasattr(content_item, 'transcript'):
                                # Display AI speech transcription
                                if content_item.transcript:
                                    print(f"🤖 AI (speech): '{content_item.transcript}'")

        except Exception as e:
            print(f"❌ Error extracting conversation text: {e}")

    async def _fallback_text_to_speech(self, event) -> None:
        """Fallback method to send a simple audio response to Twilio when AI audio extraction fails."""
        try:
            print("🔊 Fallback: Sending simple beep to Twilio")

            # Send a simple beep sound (this is a minimal μ-law audio sample)
            # This creates a short beep sound that the user will hear
            beep_samples = 8000  # 1 second at 8kHz
            beep_audio = bytearray()

            # Generate a simple beep (sine wave at 800Hz)
            import math
            for i in range(beep_samples):
                # Create a sine wave and convert to μ-law
                sample = int(32767 * math.sin(2 * math.pi * 800 * i / 8000))
                # Simple μ-law encoding (simplified)
                if sample >= 0:
                    ulaw_sample = 0xFF - sample // 128
                else:
                    ulaw_sample = 0x7F + (-sample) // 128
                beep_audio.append(ulaw_sample)

            # Send the beep audio to Twilio
            beep_base64 = base64.b64encode(bytes(beep_audio)).decode("utf-8")

            await self.twilio_websocket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": beep_base64},
                    }
                )
            )

            print(f"✅ Sent fallback beep audio to Twilio ({len(beep_audio)} bytes)")

        except Exception as e:
            print(f"❌ Error in fallback audio: {e}")

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
