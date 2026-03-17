"""
ElevenLexa — AI Voice Assistant
================================
Raspberry Pi voice assistant powered by the OpenAI Realtime API.

Architecture overview:
  AudioPlayer    streams PCM audio from OpenAI → speaker (pacat)
  AudioRecorder  captures mic → queues PCM chunks for OpenAI (pacat)
  EyeDisplay     optional pygame display on HDMI screen (display.py)
  realtime_session  manages the WebSocket connection to OpenAI

Audio pipeline:
  Mic ──► pacat --record ──► AudioRecorder.audio_queue ──► OpenAI WebSocket
  OpenAI WebSocket ──► AudioPlayer ──► pacat --playback ──► Speaker

Echo prevention:
  recorder.muted = True while the AI speaks (prevents the mic from
  picking up the speaker output). After the AI finishes, a 2.5 s
  pause lets the room echo settle, then the audio queue is flushed.

Display (optional):
  EyeDisplay renders a retro robot face on the HDMI display.
  Falls back gracefully when no display is available (SSH / headless).

Hardware:
  Raspberry Pi 4/5, Soundcore Mini Bluetooth Speaker,
  PipeWire audio server (with module-echo-cancel loaded at startup)

Start:
  python3 main.py
"""

import asyncio
import base64
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

try:
    import websockets
except ImportError:
    print("Missing dependency: sudo pip3 install --break-system-packages websockets")
    sys.exit(1)

try:
    from display import EyeDisplay, STATE_IDLE, STATE_LISTENING, STATE_SPEAKING
    DISPLAY_AVAILABLE = True
except Exception as _de:
    print(f"Display not available: {_de}")
    DISPLAY_AVAILABLE = False
    EyeDisplay = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENAI_API_KEY = "sk-"

WS_URL      = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
VOICE       = "verse"   # alloy, ash, ballad, coral, echo, fable,
                         # onyx, nova, sage, shimmer, verse
SAMPLE_RATE = 24000      # Hz — OpenAI Realtime uses 24 kHz PCM16 mono
CHUNK_MS    = 100        # ms per audio chunk sent to OpenAI
CHUNK_SIZE  = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # bytes (16-bit = 2 bytes/sample)

# PipeWire needs XDG_RUNTIME_DIR to locate the user session socket
PIPEWIRE_ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"}

INSTRUCTIONS = (
    "You are Peter, a warm and intelligent personal assistant running on a Raspberry Pi. "
    "IMPORTANT: Always respond in English or Korean only — never German or any other language. "
    "Match the language the user speaks: if they speak Korean, reply in Korean; otherwise use English. "
    "The speech recognition may sometimes be inaccurate due to microphone quality. "
    "Use context to interpret what the user likely meant, even if the transcript seems odd. "
    "If something is genuinely unclear, ask one short clarifying question. "
    "Never repeat back what the user said word-for-word. "
    "Be conversational, natural, and concise — like a smart friend, not a robot. "
    "You can help with anything: questions, tasks, conversation, reminders, ideas."
)


# ---------------------------------------------------------------------------
# Startup sound
# ---------------------------------------------------------------------------

def _make_tone(freq: float, duration_ms: int, sample_rate: int = 24000,
               amplitude: float = 0.35, fade_ms: int = 20) -> bytes:
    """Generate a PCM16 sine-wave tone with short fade-in/out to avoid clicks."""
    import math, struct
    n_samples = int(sample_rate * duration_ms / 1000)
    fade_n    = int(sample_rate * fade_ms / 1000)
    out       = bytearray()
    for i in range(n_samples):
        env = 1.0
        if i < fade_n:
            env = i / fade_n
        elif i >= n_samples - fade_n:
            env = (n_samples - i) / fade_n
        val = math.sin(2 * math.pi * freq * i / sample_rate) * amplitude * env
        out += struct.pack("<h", int(val * 32767))
    return bytes(out)


def _make_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    n_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * n_samples


def play_startup_sound():
    """Plays a short C-major arpeggio melody via pacat (no WAV file needed)."""
    print("DEBUG: Starte Startup-Chime...")
    try:
        # C5 – E5 – G5 – C6  (ascending C-major arpeggio)
        notes = [
            (523.25, 160),   # C5
            (659.25, 160),   # E5
            (783.99, 160),   # G5
            (1046.5, 320),   # C6  (held longer for finish)
        ]
        pcm = b"".join(
            _make_tone(freq, dur) + _make_silence(40)
            for freq, dur in notes
        )
        proc = subprocess.Popen(
            ["pacat", "--playback", "--format=s16le",
             "--rate=24000", "--channels=1"],
            stdin=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        proc.stdin.write(pcm)
        proc.stdin.close()
        proc.wait(timeout=5)
        print("DEBUG: Startup-Chime abgespielt.")
    except Exception as e:
        print(f"DEBUG: Startup-Sound Fehler: {e}")


# ---------------------------------------------------------------------------
# Audio output
# ---------------------------------------------------------------------------

class AudioPlayer:
    """
    Wraps `pacat --playback`.
    Receives raw PCM16 bytes and writes them to pacat stdin →
    PipeWire routes them to the default sink (Bluetooth speaker).
    """

    def __init__(self):
        self.process = None

    def start(self):
        """Start the pacat playback process."""
        self.process = subprocess.Popen(
            ["pacat", "--playback", "--format=s16le",
             f"--rate={SAMPLE_RATE}", "--channels=1"],
            stdin=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._log_stderr, daemon=True).start()
        time.sleep(0.3)
        if self.process.poll() is not None:
            print(f"DEBUG: KRITISCH - pacat-play sofort beendet! Exit Code: {self.process.poll()}")
        else:
            print(f"DEBUG: pacat-play gestartet (PID: {self.process.pid})")

    def _log_stderr(self):
        for line in self.process.stderr:
            txt = line.decode(errors="replace").strip()
            if txt:
                print(f"DEBUG: pacat-play: {txt}", flush=True)

    def write(self, audio_bytes: bytes):
        """Write a PCM16 chunk to the playback stream."""
        if self.process and self.process.stdin and self.process.poll() is None:
            try:
                self.process.stdin.write(audio_bytes)
                self.process.stdin.flush()
            except Exception as e:
                print(f"DEBUG: Player write error: {e}", flush=True)

    def stop(self):
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Audio input
# ---------------------------------------------------------------------------

class AudioRecorder:
    """
    Wraps `pacat --record`.
    Continuously reads PCM16 chunks from the mic into audio_queue.
    Set muted=True to discard incoming audio (used during AI speech
    to prevent echo feedback into the API).
    """

    def __init__(self):
        self.process     = None
        self.audio_queue = queue.Queue()
        self._running    = False
        self.muted       = False

    def start(self):
        """Start the pacat recording process and reader thread."""
        self._running = True
        self.process = subprocess.Popen(
            ["pacat", "--record", "--format=s16le",
             f"--rate={SAMPLE_RATE}", "--channels=1"],
            stdout=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._log_stderr, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()
        print(f"DEBUG: pacat-record gestartet (PID: {self.process.pid})")

    def _log_stderr(self):
        for line in self.process.stderr:
            txt = line.decode(errors="replace").strip()
            if txt:
                print(f"DEBUG: pacat-record: {txt}", flush=True)

    def _read_loop(self):
        """Background thread: reads mic chunks and enqueues them."""
        while self._running and self.process and self.process.poll() is None:
            try:
                data = self.process.stdout.read(CHUNK_SIZE)
                if data and not self.muted:
                    self.audio_queue.put(data)
            except Exception as e:
                print(f"DEBUG: Mic-Read Fehler: {e}", flush=True)
                break
        print("DEBUG: Mic-Thread beendet.", flush=True)

    def stop(self):
        self._running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# OpenAI Realtime session
# ---------------------------------------------------------------------------

async def realtime_session(
    player: AudioPlayer,
    recorder: AudioRecorder,
    stop_event: asyncio.Event,
    display=None
):
    """
    Manages one WebSocket session with the OpenAI Realtime API.

    Sends mic audio to OpenAI and plays back the AI's audio response.
    Handles all API events and updates the display state accordingly.

    Args:
        player:     AudioPlayer — speaker output
        recorder:   AudioRecorder — mic input
        stop_event: asyncio.Event — set to trigger graceful shutdown
        display:    Optional EyeDisplay for HDMI output
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    print("DEBUG: Verbinde mit OpenAI Realtime API...")
    async with websockets.connect(WS_URL, additional_headers=headers, max_size=None) as ws:
        print("DEBUG: Verbunden!")

        # Configure the session parameters
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "voice": VOICE,
                "instructions": INSTRUCTIONS,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 500,   # ms of audio kept before speech onset
                    "silence_duration_ms": 900,  # ms of silence before turn ends
                },
            }
        }))

        # Trigger Peter's opening greeting immediately
        await ws.send(json.dumps({"type": "response.create"}))

        ai_speaking = False
        loop = asyncio.get_event_loop()

        async def send_audio():
            """Reads mic chunks from queue and streams them to OpenAI."""
            while not stop_event.is_set():
                try:
                    audio_data = await loop.run_in_executor(
                        None, lambda: recorder.audio_queue.get(timeout=0.05)
                    )
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio_data).decode(),
                    }))
                except queue.Empty:
                    pass
                except Exception as e:
                    print(f"DEBUG: Send-Audio Fehler: {e}", flush=True)
                    break

        async def receive_events():
            """Handles all incoming events from the OpenAI Realtime API."""
            nonlocal ai_speaking
            async for message in ws:
                if stop_event.is_set():
                    break
                event = json.loads(message)
                etype = event.get("type", "")

                if etype == "response.audio.delta":
                    # AI is sending audio — play it and mute mic to prevent echo
                    if not ai_speaking:
                        ai_speaking = True
                        recorder.muted = True
                        print("\nDEBUG: KI spricht — Mikrofon stumm.", flush=True)
                        if display:
                            display.set_state(STATE_SPEAKING)
                            display.clear_text()
                    audio_bytes = base64.b64decode(event["delta"])
                    await loop.run_in_executor(None, player.write, audio_bytes)

                elif etype == "response.audio_transcript.delta":
                    # Streaming text transcript of AI speech → update display live
                    if display:
                        display.append_text(event.get("delta", ""))

                elif etype == "response.done":
                    # AI finished — wait for echo to settle, then unmute mic
                    ai_speaking = False
                    if display:
                        display.set_state(STATE_IDLE)
                    await asyncio.sleep(2.5)
                    flushed = 0
                    while not recorder.audio_queue.empty():
                        try:
                            recorder.audio_queue.get_nowait()
                            flushed += 1
                        except queue.Empty:
                            break
                    if flushed:
                        print(f"DEBUG: {flushed} Echo-Chunks verworfen.", flush=True)
                    recorder.muted = False
                    print("DEBUG: KI fertig — Mikrofon aktiv.", flush=True)

                elif etype == "response.audio_transcript.done":
                    # Full transcript available — show final text on display
                    transcript = event.get("transcript", "")
                    print(f"\nAgent: {transcript}", flush=True)
                    if display:
                        display.set_text(transcript)

                elif etype == "conversation.item.input_audio_transcription.completed":
                    # Whisper transcript of the user's speech
                    print(f"\nDu: {event.get('transcript', '')}", flush=True)

                elif etype == "input_audio_buffer.speech_started":
                    # VAD detected user starting to speak
                    print("\nDEBUG: Sprache erkannt...", flush=True)
                    if display:
                        display.set_state(STATE_LISTENING)
                        display.clear_text()

                elif etype == "error":
                    print(f"\nFEHLER von OpenAI: {json.dumps(event, ensure_ascii=False)}", flush=True)

                elif etype not in {
                    # Known informational events — no action needed
                    "session.created", "session.updated", "response.created",
                    "response.output_item.added", "response.content_part.added",
                    "response.content_part.done", "response.output_item.done",
                    "input_audio_buffer.speech_stopped", "input_audio_buffer.committed",
                    "conversation.item.created", "rate_limits.updated",
                    "response.audio.done", "response.audio_transcript.delta",
                    "conversation.item.input_audio_transcription.delta",
                }:
                    print(f"DEBUG: Unbekanntes Event: {etype}", flush=True)

        # Run send and receive concurrently; cancel both cleanly on shutdown
        t1 = asyncio.create_task(send_audio())
        t2 = asyncio.create_task(receive_events())
        await stop_event.wait()
        t1.cancel()
        t2.cancel()
        await ws.close()
        await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    play_startup_sound()
    print("Initialisiere ElevenLexa (OpenAI Voice Mode)...")

    display = EyeDisplay() if DISPLAY_AVAILABLE else None
    if display:
        display.start()

    player   = AudioPlayer()
    recorder = AudioRecorder()
    player.start()
    recorder.start()

    print("--- Voice-Modus aktiv. Sprich mit Peter! (Ctrl+C zum Beenden) ---")

    loop       = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def shutdown(sig, frame):
        print("\nBeende...")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    async def run_with_reconnect():
        """Reconnects automatically if the WebSocket connection drops."""
        while not stop_event.is_set():
            try:
                await realtime_session(player, recorder, stop_event, display)
            except Exception as e:
                if stop_event.is_set():
                    break
                print(f"DEBUG: Verbindung unterbrochen ({e}), reconnect in 3s...", flush=True)
                await asyncio.sleep(3)

    try:
        loop.run_until_complete(run_with_reconnect())
    except Exception as e:
        import traceback
        print(f"Fehler: {e}")
        traceback.print_exc()
    finally:
        player.stop()
        recorder.stop()
        if display:
            display.stop()
        loop.close()


if __name__ == "__main__":
    main()
