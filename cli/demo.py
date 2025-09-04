import asyncio
import os
import queue
import sys
import threading
from typing import Any, Optional

import numpy as np
import sounddevice as sd

from news_agent import get_starting_agent
from agents.realtime import (
    RealtimeAgent,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)

# =========================
# Audio / Buffer Settings
# =========================
CHUNK_LENGTH_S = 0.02        # 20 ms frames (telephony/WebRTC standard)
SAMPLE_RATE = 24000          # Use 16000 for best ASR alignment; switch to 24000 if your server requires it
FORMAT = np.int16
CHANNELS = 1

# Playback robustness
PREROLL_CHUNKS = 8           # Wait until we have ~160 ms of audio before starting playback
MAX_QUEUE = 128              # Jitter buffer depth (input to speaker)

# =========================
# Security: API key via env
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    # Fail early & loudly; do NOT hardcode keys
    raise RuntimeError("OPENAI_API_KEY is not set in environment")
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY


def _truncate_str(s: str, max_length: int) -> str:
    return s if len(s) <= max_length else s[:max_length] + "..."


class NoUIDemo:
    """
    Robust full-duplex(ish) audio client with half-duplex gating to prevent overlap.
    - Callback mic input -> thread-safe send to session
    - Output callback pulls from jitter buffer with preroll
    """

    def __init__(self) -> None:
        # Realtime session
        self.session: Optional[RealtimeSession] = None
        self.runner: Optional[RealtimeRunner] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Sounddevice streams
        self.audio_in: Optional[sd.InputStream] = None
        self.audio_out: Optional[sd.OutputStream] = None

        # Output state
        self.output_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=MAX_QUEUE)
        self.current_audio_chunk: Optional[np.ndarray] = None
        self.chunk_position: int = 0
        self.interrupt_event = threading.Event()
        self.ready_to_play = False  # wait for preroll before unmuting output

        # Half-duplex gating to avoid the agent hearing its own TTS
        self.is_playing_tts = False
        self.block_mic = False

        # Derived constants
        self.blocksize = int(SAMPLE_RATE * CHUNK_LENGTH_S)  # samples per frame (e.g., 320 at 16k/20ms)

    # =========================
    # Output (speaker) callback
    # =========================
    def _output_callback(self, outdata, frames: int, time, status) -> None:
        if status:
            # print(f"[Output] {status}")  # Uncomment for debugging
            pass

        # On explicit interrupt: flush buffer and reset positions
        if self.interrupt_event.is_set():
            with self.output_queue.mutex:
                self.output_queue.queue.clear()
            self.current_audio_chunk = None
            self.chunk_position = 0
            self.interrupt_event.clear()
            self.ready_to_play = False  # re-preroll on next utterance
            outdata.fill(0)
            return

        # Honor preroll: keep silence until we have enough buffered audio
        if not self.ready_to_play:
            # If we have enough in queue, flip the flag
            if self.output_queue.qsize() >= PREROLL_CHUNKS:
                self.ready_to_play = True
            else:
                outdata.fill(0)
                return

        # Normal playback
        outdata.fill(0)
        buf = outdata[:, 0]  # mono view
        samples_filled = 0

        while samples_filled < frames:
            if self.current_audio_chunk is None:
                try:
                    self.current_audio_chunk = self.output_queue.get_nowait()
                    self.chunk_position = 0
                except queue.Empty:
                    # Underrun — keep remaining as silence
                    break

            remaining_out = frames - samples_filled
            remaining_chunk = len(self.current_audio_chunk) - self.chunk_position
            n = min(remaining_out, remaining_chunk)
            if n > 0:
                start_o = samples_filled
                end_o = samples_filled + n
                start_c = self.chunk_position
                end_c = self.chunk_position + n
                buf[start_o:end_o] = self.current_audio_chunk[start_c:end_c]
                samples_filled += n
                self.chunk_position += n

            if self.chunk_position >= len(self.current_audio_chunk):
                self.current_audio_chunk = None
                self.chunk_position = 0

    # =========================
    # Input (mic) callback
    # =========================
    def _input_callback(self, indata, frames: int, time, status) -> None:
        if status:
            # print(f"[Input] {status}")  # Uncomment for debugging
            pass

        # Half-duplex: while TTS is playing, suppress mic to avoid feedback/overlap
        if self.block_mic or self.is_playing_tts:
            return

        if not self.session or not self.loop:
            return

        try:
            # Ensure int16 mono bytes
            mono = indata[:, 0] if indata.ndim > 1 else indata
            if mono.dtype != np.int16:
                # Cast defensively if device returns float32
                mono = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
            audio_bytes = mono.tobytes()

            # Send to session from callback thread
            asyncio.run_coroutine_threadsafe(self.session.send_audio(audio_bytes), self.loop)
        except Exception:
            pass

    # =========================
    # Main run
    # =========================
    async def run(self) -> None:
        print("Connecting…")

        # Save loop for thread-safe sends
        self.loop = asyncio.get_running_loop()

        # Prepare audio output
        self.audio_out = sd.OutputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=FORMAT,
            blocksize=self.blocksize,
            latency="low",
            callback=self._output_callback,
        )
        self.audio_out.start()

        try:
            agent = get_starting_agent()
            self.runner = RealtimeRunner(agent)

            async with await self.runner.run() as session:
                self.session = session
                print("Connected. Starting mic…")

                # Start mic with callback
                self.audio_in = sd.InputStream(
                    channels=CHANNELS,
                    samplerate=SAMPLE_RATE,
                    dtype=FORMAT,
                    blocksize=self.blocksize,
                    latency="low",
                    callback=self._input_callback,
                )
                self.audio_in.start()

                print("Live. Speak when ready. (Half-duplex: mic mutes during TTS)")

                # Consume session events
                async for event in session:
                    await self._on_event(event)

        finally:
            # Clean up audio
            try:
                if self.audio_in and self.audio_in.active:
                    self.audio_in.stop()
                if self.audio_in:
                    self.audio_in.close()
            except Exception:
                pass

            try:
                if self.audio_out and self.audio_out.active:
                    self.audio_out.stop()
                if self.audio_out:
                    self.audio_out.close()
            except Exception:
                pass

            print("Session ended")

    # =========================
    # Event handling
    # =========================
    async def _on_event(self, event: RealtimeSessionEvent) -> None:
        try:
            etype = event.type

            if etype == "agent_start":
                # New utterance starting soon: prep state
                self.is_playing_tts = True
                self.block_mic = True   # pause mic while TTS streams
                # Do NOT clear queue yet; we need preroll

            elif etype == "audio":
                # Queue TTS audio; do not drop unless we're deeply backed up
                np_audio = np.frombuffer(event.audio.data, dtype=np.int16)

                try:
                    self.output_queue.put_nowait(np_audio)
                except queue.Full:
                    # Drop oldest to keep most recent flowing
                    try:
                        _ = self.output_queue.get_nowait()
                        self.output_queue.put_nowait(np_audio)
                    except queue.Empty:
                        pass

                # When enough buffered, allow playback to start
                if not self.ready_to_play and self.output_queue.qsize() >= PREROLL_CHUNKS:
                    self.ready_to_play = True

            elif etype == "audio_end":
                # End of this TTS segment
                self.is_playing_tts = False
                self.block_mic = False
                # Let any residual buffered audio drain naturally; next utterance will re-preroll

            elif etype == "audio_interrupted":
                # Interrupt current audio; clear output immediately
                self.is_playing_tts = False
                self.block_mic = False
                self.interrupt_event.set()

            elif etype == "tool_start":
                pass
            elif etype == "tool_end":
                pass
            elif etype == "handoff":
                pass
            elif etype == "history_updated":
                pass
            elif etype == "history_added":
                pass
            elif etype == "raw_model_event":
                # Debug hook
                # print(f"Raw: {_truncate_str(str(event.data), 120)}")
                pass
            elif etype == "error":
                print(f"[Error] {event.error}")
            elif etype == "agent_end":
                pass
            else:
                # print(f"[Info] Unknown event: {etype}")
                pass

        except Exception as e:
            print(f"[Event Error] {_truncate_str(str(e), 200)}")


if __name__ == "__main__":
    demo = NoUIDemo()
    try:
        asyncio.run(demo.run())
    except KeyboardInterrupt:
        print("\nExiting…")
        sys.exit(0)
